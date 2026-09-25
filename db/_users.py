"""User CRUD, accessibility, login attempts, and editor access."""

import json
import re
import string
import secrets
from datetime import datetime, timedelta, timezone

from ._connection import get_db_context, retry_on_busy


def _gen_user_id():
    """Generate a random 8-character alphanumeric lowercase user ID."""
    chars = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(8))


def generate_random_id(length=12):
    """Generate a random alphanumeric ID for entities.

    This function generates cryptographically secure random IDs that can be used
    for groups, pages, categories, and other entities as an alternative to
    sequential INTEGER AUTOINCREMENT IDs.

    Args:
        length (int): Length of the ID to generate (default: 12 characters)

    Returns:
        str: Random alphanumeric lowercase ID

    Note:
        Using random IDs instead of sequential integers provides:
        - Better privacy (can't enumerate all entities by incrementing IDs)
        - Harder to guess entity counts or creation patterns
        - More suitable for public-facing URLs

        However, migrating existing entities from INTEGER to TEXT IDs requires:
        - Database schema changes (PRIMARY KEY type change)
        - Foreign key relationship updates
        - Route parameter type changes (<int:id> to <string:id>)
        - Extensive testing to prevent regressions

        For this reason, this function is provided as a foundation for future
        migration work, but not yet integrated into all entity creation paths.
    """
    chars = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


def is_suspension_active(user):
    """Return True if the user is currently suspended.

    Handles both permanent suspensions (``suspended_until`` is NULL) and timed
    suspensions (``suspended_until`` is a future UTC ISO datetime).  If the
    suspension has expired this function returns False but does **not** modify
    the database, call :func:`check_suspension_expired` to auto-unsuspend.
    """
    if not user or not user["suspended"]:
        return False
    suspended_until = user["suspended_until"]
    if not suspended_until:
        # Permanent suspension
        return True
    try:
        expiry = datetime.fromisoformat(suspended_until.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True
    return datetime.now(timezone.utc) < expiry


def check_suspension_expired(user_id):
    """Auto-unsuspend a user whose timed suspension has elapsed.

    Returns True if the user was unsuspended, False otherwise.
    """
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT suspended, suspended_until FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not row or not row["suspended"]:
            return False
        suspended_until = row["suspended_until"]
        if not suspended_until:
            # Permanent suspension: do not auto-unsuspend
            return False
        try:
            expiry = datetime.fromisoformat(suspended_until.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return False
        if datetime.now(timezone.utc) >= expiry:
            conn.execute(
                "UPDATE users SET suspended=0, suspended_until=NULL, "
                "suspend_reason=NULL, suspend_reason_visible=0, suspend_time_visible=0 "
                "WHERE id=?",
                (user_id,),
            )
            conn.commit()
            return True
        return False


def _insert_user(conn, username, hashed_pw, role="user", invite_code=None,
                 approval_status="approved", custom_role_id=None):
    """Insert a user in the caller's transaction without committing it."""
    uid = _gen_user_id()
    while conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        uid = _gen_user_id()
    conn.execute(
        "INSERT INTO users (id, username, password, role, invite_code, approval_status, custom_role_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, username, hashed_pw, role, invite_code, approval_status, custom_role_id),
    )
    return uid


def create_user(username, hashed_pw, role="user", invite_code=None):
    """Create an operator-created user and return its generated ID."""
    with get_db_context() as conn:
        uid = _insert_user(conn, username, hashed_pw, role, invite_code)
        conn.commit()
    return uid


def create_signup_user(username, hashed_pw, *, invite_code=None, approval_required=False):
    """Publish the account, role, approval state, and invite redemption together."""
    from ._invites import _get_valid_invite, _consume_invite

    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        invite = None
        role = "user"
        custom_role_id = None
        if invite_code is not None:
            invite = _get_valid_invite(conn, invite_code)
            if invite is None:
                raise ValueError("Invalid or expired invite code.")
            if invite["assigned_role"] in {"user", "editor", "admin"}:
                role = invite["assigned_role"]
            if invite["assigned_custom_role_id"]:
                custom_role = conn.execute(
                    "SELECT id, base_role FROM custom_roles WHERE id=?",
                    (invite["assigned_custom_role_id"],),
                ).fetchone()
                if custom_role:
                    custom_role_id, role = custom_role["id"], custom_role["base_role"]
        uid = _insert_user(conn, username, hashed_pw, role, invite["code"] if invite else None,
                           "pending" if approval_required else "approved", custom_role_id)
        if invite:
            _consume_invite(conn, invite, uid)
        conn.commit()
        return uid


@retry_on_busy
def get_user_by_id(user_id):
    """Return the user row for the given *user_id*, or None if not found.

    Wrapped in :func:`retry_on_busy` because this lookup runs on every
    authenticated request via :func:`helpers.get_current_user`.  A
    transient ``database is locked`` from a concurrent write (periodic
    cleanup, nightly backup, another worker committing a page edit) used
    to surface as the "random 500 that goes away after a reload"
    pattern.
    """
    with get_db_context() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return user


@retry_on_busy
def get_user_by_username(username):
    """Return the user row for the given *username* (case-insensitive), or None.

    Wrapped in :func:`retry_on_busy` so a transient SQLite lock during
    login does not surface to the user as a 500 on the login form.
    """
    with get_db_context() as conn:
        user = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    return user


_ALLOWED_USER_COLUMNS = {
    "username", "password", "role", "suspended", "suspended_until", "last_login_at",
    "session_token", "reserved_pages_quota",
    "suspend_reason", "suspend_reason_visible", "suspend_time_visible",
    "custom_role_id",
    "original_password_backup", "is_superuser",
    "force_password_change",
    "onboarding_required", "onboarding_completed_at",
    "intro_required", "intro_completed_at",
    "api_access_enabled",
    "userbot_enabled", "userbot_mode_lock",
    "userbot_enable_count", "userbot_disable_count",
    "approval_status", "denied_at", "denied_notified",
    "approved_by", "denied_by", "approved_denied_at",
}


def update_user(user_id, **kwargs):
    """Update one or more user columns for the given *user_id*.

    Only columns listed in ``_ALLOWED_USER_COLUMNS`` may be changed;
    any unknown column name raises :exc:`ValueError`.
    """
    if not user_id:
        raise ValueError("user_id is required")
    for k in kwargs:
        if k not in _ALLOWED_USER_COLUMNS:
            raise ValueError(f"Invalid column: {k}")
    if not kwargs:
        return
    with get_db_context() as conn:
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [user_id]
        conn.execute(f"UPDATE users SET {sets} WHERE id=?", vals)
        conn.commit()


def delete_user(user_id):
    """Delete a user and clean up their invite code usage."""
    if not user_id:
        raise ValueError("user_id is required")
    with get_db_context() as conn:
        # Check if used_by column still exists (it might if migration hasn't run yet in some edge cases,
        # but in our new schema it doesn't).
        # Actually, during migration we already moved data to invite_code_usage.
        # To be safe and compatible with the new schema:
        conn.execute("DELETE FROM invite_code_usage WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()


def record_username_change(user_id, old_username, new_username):
    """Record a username change in the history table."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO username_history (user_id, old_username, new_username, changed_at) VALUES (?, ?, ?, ?)",
            (user_id, old_username, new_username, now),
        )
        conn.commit()


@retry_on_busy
def get_username_history(user_id):
    """Return all username changes for a user, newest first."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT * FROM username_history WHERE user_id=? ORDER BY changed_at DESC",
            (user_id,),
        ).fetchall()
    return rows


def _escape_like(value):
    """Escape SQLite LIKE metacharacters so they are matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def update_username_mentions(old_username, new_username):
    """Update all @username mentions across all wiki content.

    If new_username is None, mentions are replaced with '@account deleted'.
    """
    import sqlite3
    replacement = f"@{new_username}" if new_username else "@account deleted"
    old_mention = f"@{old_username}"

    # Regex to match @username with word boundaries
    pattern = re.compile(re.escape(old_mention) + r"(?![A-Za-z0-9_-])", re.IGNORECASE)

    with get_db_context() as conn:
        # 1. Pages
        # Use a case-insensitive LIKE or just fetch rows that might match
        rows = conn.execute("SELECT id, content FROM pages WHERE content LIKE ? ESCAPE '\\'", (f"%{_escape_like(old_mention)}%",)).fetchall()
        for row in rows:
            new_content = pattern.sub(replacement, row["content"])
            if new_content != row["content"]:
                conn.execute("UPDATE pages SET content=? WHERE id=?", (new_content, row["id"]))

        # 2. Page history
        rows = conn.execute("SELECT id, content FROM page_history WHERE content LIKE ? ESCAPE '\\'", (f"%{_escape_like(old_mention)}%",)).fetchall()
        for row in rows:
            new_content = pattern.sub(replacement, row["content"])
            if new_content != row["content"]:
                conn.execute("UPDATE page_history SET content=? WHERE id=?", (new_content, row["id"]))

        # 3. Drafts
        rows = conn.execute("SELECT id, content FROM drafts WHERE content LIKE ? ESCAPE '\\'", (f"%{_escape_like(old_mention)}%",)).fetchall()
        for row in rows:
            new_content = pattern.sub(replacement, row["content"])
            if new_content != row["content"]:
                conn.execute("UPDATE drafts SET content=? WHERE id=?", (new_content, row["id"]))

        # 4. Announcements
        rows = conn.execute("SELECT id, content FROM announcements WHERE content LIKE ? ESCAPE '\\'", (f"%{_escape_like(old_mention)}%",)).fetchall()
        for row in rows:
            new_content = pattern.sub(replacement, row["content"])
            if new_content != row["content"]:
                conn.execute("UPDATE announcements SET content=? WHERE id=?", (new_content, row["id"]))

        # 5. Kanban (optional plugin)
        try:
            rows = conn.execute("SELECT id, description FROM kanban_tickets WHERE description LIKE ? ESCAPE '\\'", (f"%{_escape_like(old_mention)}%",)).fetchall()
            for row in rows:
                new_content = pattern.sub(replacement, row["description"])
                if new_content != row["description"]:
                    conn.execute("UPDATE kanban_tickets SET description=? WHERE id=?", (new_content, row["id"]))

            rows = conn.execute("SELECT id, content FROM kanban_ticket_comments WHERE content LIKE ? ESCAPE '\\'", (f"%{_escape_like(old_mention)}%",)).fetchall()
            for row in rows:
                new_content = pattern.sub(replacement, row["content"])
                if new_content != row["content"]:
                    conn.execute("UPDATE kanban_ticket_comments SET content=? WHERE id=?", (new_content, row["id"]))
        except sqlite3.OperationalError:
            pass

        # 6. Chats (optional plugin)
        try:
            rows = conn.execute("SELECT id, content FROM chat_messages WHERE content LIKE ? ESCAPE '\\'", (f"%{_escape_like(old_mention)}%",)).fetchall()
            for row in rows:
                new_content = pattern.sub(replacement, row["content"])
                if new_content != row["content"]:
                    conn.execute("UPDATE chat_messages SET content=? WHERE id=?", (new_content, row["id"]))

            rows = conn.execute("SELECT id, content FROM group_messages WHERE content LIKE ? ESCAPE '\\'", (f"%{_escape_like(old_mention)}%",)).fetchall()
            for row in rows:
                new_content = pattern.sub(replacement, row["content"])
                if new_content != row["content"]:
                    conn.execute("UPDATE group_messages SET content=? WHERE id=?", (new_content, row["id"]))
        except sqlite3.OperationalError:
            pass

        conn.commit()


# Per-user display preferences. Every key in _A11Y_DEFAULTS must have a matching
# users column, and a missing or NULL column falls back to the default below.
_A11Y_DEFAULTS = {
    "theme_mode": "default",
    "interface_language": "default",
    "font_scale": 1.0,
    "contrast": 0,
    "sidebar_width": 250,
    "content_max_width": 0,
    "editor_pane_width": 0,
    "editor_height": 0,
    "custom_bg": "",
    "custom_text": "",
    "custom_primary": "",
    "custom_secondary": "",
    "custom_accent": "",
    "custom_sidebar": "",
    "background_image": "",
    "line_height": 0,
    "letter_spacing": 0,
    "reduce_motion": 0,
    # Semantic Highlighting: make selected Markdown semantics
    # (bold/italic/code/link/heading) visually more distinct.  0 = off,
    # 1 = subtle, 2 = strong.  Applies only inside ``.wiki-content``.
    "semantic_bold": 0,
    "semantic_italic": 0,
    "semantic_code": 0,
    "semantic_link": 0,
    "semantic_heading": 0,
    "sidebar_apps_order": "",
}

_A11Y_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_A11Y_RGB_COLOR_RE = re.compile(r"^rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)$")
_A11Y_BACKGROUND_IMAGE_RE = re.compile(r"^backgrounds/[0-9a-f]{32}\.jpe?g$")


def _clean_a11y_pref(key, value):
    """Return a sanitised accessibility preference value for *key*."""
    if key == "background_image":
        if not isinstance(value, str):
            return ""
        value = value.strip()
        if not value:
            return ""
        return value if _A11Y_BACKGROUND_IMAGE_RE.fullmatch(value) else ""
    if not key.startswith("custom_"):
        return value
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value:
        return ""
    if _A11Y_HEX_COLOR_RE.fullmatch(value) or _A11Y_RGB_COLOR_RE.fullmatch(value):
        return value
    return ""


@retry_on_busy
def get_user_accessibility(user_id):
    """Return the accessibility preferences dict for a user (merged with defaults).

    Wrapped in :func:`retry_on_busy` because :func:`app.inject_globals`
    reads accessibility prefs on every authenticated request (to resolve
    interface language).  A transient ``database is locked`` here used to
    surface as the "random 500 that goes away after a reload" pattern.
    """
    with get_db_context() as conn:
        row = conn.execute("SELECT accessibility FROM users WHERE id=?", (user_id,)).fetchone()
    result = dict(_A11Y_DEFAULTS)
    if row and row["accessibility"]:
        try:
            saved = json.loads(row["accessibility"])
            result.update({
                k: _clean_a11y_pref(k, v)
                for k, v in saved.items()
                if k in _A11Y_DEFAULTS
            })
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def save_user_accessibility(user_id, prefs):
    """Persist accessibility preferences for a user."""
    cleaned = {
        k: _clean_a11y_pref(k, prefs[k])
        for k in _A11Y_DEFAULTS
        if k in prefs
    }
    with get_db_context() as conn:
        conn.execute("UPDATE users SET accessibility=? WHERE id=?",
                     (json.dumps(cleaned), user_id))
        conn.commit()


def record_login_attempt(ip):
    """Insert a failed login attempt record for the given *ip* address."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO login_attempts (ip, attempted_at) VALUES (?, ?)", (ip, now))
        conn.commit()


def count_recent_login_attempts(ip, window_seconds):
    """Return count of attempts from IP within the last window_seconds."""
    with get_db_context() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
        # Prune old entries to keep table small
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
        cnt = conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip=? AND attempted_at >= ?",
            (ip, cutoff),
        ).fetchone()[0]
        conn.commit()
    return cnt


def clear_login_attempts(ip):
    """Remove all failed login attempt records for the given *ip* address."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
        conn.commit()


def clear_all_login_attempts():
    """Remove all failed login attempt records from the database."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM login_attempts")
        conn.commit()


# Rate limit counters are kept in the database, not in process memory, so all
# Gunicorn workers throttle against the same per-IP, per-bucket tally.

def record_rate_limit_hit(ip, bucket):
    """Insert a rate limit hit record for the given *ip* and *bucket*."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO rate_limit_hits (ip, bucket, hit_at) VALUES (?, ?, ?)",
            (ip, bucket, now),
        )
        conn.commit()


def count_recent_rate_limit_hits(ip, bucket, window_seconds):
    """Return count of hits from *ip*+*bucket* within the last *window_seconds*.

    Also prunes expired records for this ip+bucket to keep the table small.
    """
    with get_db_context() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
        # Prune old entries to keep table small
        conn.execute(
            "DELETE FROM rate_limit_hits WHERE ip=? AND bucket=? AND hit_at < ?",
            (ip, bucket, cutoff),
        )
        cnt = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_hits WHERE ip=? AND bucket=? AND hit_at >= ?",
            (ip, bucket, cutoff),
        ).fetchone()[0]
        conn.commit()
    return cnt


def check_and_record_rate_limit_hit(ip, bucket, max_requests, window_seconds):
    """Atomically check and record a rate limit hit.

    Returns True if the request is within the limit and the hit was recorded,
    or False if the limit was already reached (hit is NOT recorded in that case).

    Uses ``BEGIN IMMEDIATE`` so that the prune + count + optional insert is a
    single serialised write transaction across all Gunicorn workers sharing the
    same SQLite database.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
        conn.execute("BEGIN IMMEDIATE")
        # Prune expired records to keep the table small
        conn.execute(
            "DELETE FROM rate_limit_hits WHERE ip=? AND bucket=? AND hit_at < ?",
            (ip, bucket, cutoff),
        )
        cnt = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_hits WHERE ip=? AND bucket=? AND hit_at >= ?",
            (ip, bucket, cutoff),
        ).fetchone()[0]
        if cnt < max_requests:
            conn.execute(
                "INSERT INTO rate_limit_hits (ip, bucket, hit_at) VALUES (?, ?, ?)",
                (ip, bucket, now),
            )
            conn.commit()
            return True
        conn.commit()
        return False


def clear_all_rate_limit_hits():
    """Remove all rate limit hit records from the database."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM rate_limit_hits")
        conn.commit()


def prune_stale_rate_limit_hits(max_age_seconds=3600):
    """Delete all rate_limit_hits rows older than *max_age_seconds*, across
    every IP and bucket.

    Per-request pruning in :func:`check_and_record_rate_limit_hit` only cleans
    up rows for the specific (ip, bucket) pair that is currently being checked.
    This function provides a global sweep so that rows from IPs that never
    return (e.g. during a DDoS) do not accumulate indefinitely between
    invocations of the weekly full cleanup.

    Returns the number of rows removed.
    """
    with get_db_context() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
        cur = conn.execute(
            "DELETE FROM rate_limit_hits WHERE hit_at < ?",
            (cutoff,),
        )
        count = cur.rowcount
        conn.commit()
    return count


@retry_on_busy
def list_users(role_filter=None, status_filter=None, approval_filter=None):
    """Return a list of all users, optionally filtered by *role_filter*,
    *status_filter*, and/or *approval_filter*.

    *status_filter* accepts ``'active'`` or ``'suspended'``.
    *approval_filter* accepts ``'pending'``, ``'approved'``, or ``'denied'``.
    """
    with get_db_context() as conn:
        q = (
            "SELECT users.*, EXISTS("
            "SELECT 1 FROM reservation_quota_requests rqr "
            "WHERE rqr.user_id = users.id AND rqr.status='pending'"
            ") AS has_pending_reservation_quota_request "
            "FROM users WHERE 1=1"
        )
        params = []
        if role_filter:
            q += " AND role=?"
            params.append(role_filter)
        if status_filter == "active":
            q += " AND suspended=0"
        elif status_filter == "suspended":
            q += " AND suspended=1"
        if approval_filter in ("pending", "approved", "denied"):
            q += " AND approval_status=?"
            params.append(approval_filter)
        q += " ORDER BY created_at"
        users = conn.execute(q, params).fetchall()
    return users


@retry_on_busy
def count_admins():
    """Return the number of active (non-suspended) admin/owner accounts."""
    with get_db_context() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role IN ('admin','owner') AND suspended=0"
        ).fetchone()[0]
    return cnt


def count_owners():
    """Return the number of active (non-suspended) owner accounts."""
    with get_db_context() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='owner' AND suspended=0"
        ).fetchone()[0]
    return cnt


def get_editor_access(user_id):
    """Return the category access settings for an editor.

    Returns a dict with:
      - ``restricted`` (bool): True if the editor is limited to specific categories.
      - ``allowed_category_ids`` (list[int]): The IDs of categories the editor may access
        (only meaningful when ``restricted`` is True).
    """
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT restricted FROM user_category_access WHERE user_id=? AND access_type='write'",
            (user_id,),
        ).fetchone()
        if row is not None:
            restricted = bool(row["restricted"])
            if restricted:
                rows = conn.execute(
                    "SELECT category_id FROM user_allowed_categories "
                    "WHERE user_id=? AND access_type='write'",
                    (user_id,),
                ).fetchall()
                allowed_ids = [r["category_id"] for r in rows]
            else:
                allowed_ids = []
            return {"restricted": restricted, "allowed_category_ids": allowed_ids}

        row = conn.execute(
            "SELECT restricted FROM editor_category_access WHERE user_id=?", (user_id,)
        ).fetchone()
        restricted = bool(row["restricted"]) if row else False
        if restricted:
            rows = conn.execute(
                "SELECT category_id FROM editor_allowed_categories WHERE user_id=?", (user_id,)
            ).fetchall()
            allowed_ids = [r["category_id"] for r in rows]
        else:
            allowed_ids = []

        if row is not None:
            conn.execute(
                "INSERT OR REPLACE INTO user_category_access (user_id, access_type, restricted) "
                "VALUES (?, 'write', ?)",
                (user_id, 1 if restricted else 0),
            )
            conn.execute(
                "DELETE FROM user_allowed_categories WHERE user_id=? AND access_type='write'",
                (user_id,),
            )
            if restricted and allowed_ids:
                conn.executemany(
                    "INSERT OR IGNORE INTO user_allowed_categories (user_id, category_id, access_type) "
                    "VALUES (?, ?, 'write')",
                    [(user_id, category_id) for category_id in allowed_ids],
                )
            conn.commit()
        return {"restricted": restricted, "allowed_category_ids": allowed_ids}


def set_user_chat_disabled(user_id, disabled):
    """Enable or disable the chat feature for a user."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE users SET chat_disabled=? WHERE id=?",
            (1 if disabled else 0, user_id),
        )
        conn.commit()


@retry_on_busy
def is_user_chat_disabled(user_id):
    """Return True if the user has been disabled from using chat."""
    with get_db_context() as conn:
        row = conn.execute("SELECT chat_disabled FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return False
    return bool(row["chat_disabled"])


def mass_logout_all_users():
    """Invalidate every user's session token, forcing a re-login on next request.

    Returns the number of rows updated.
    """
    with get_db_context() as conn:
        conn.execute(
            "UPDATE user_sessions SET revoked_at=datetime('now') "
            "WHERE revoked_at IS NULL"
        )
        cur = conn.execute("UPDATE users SET session_token=NULL WHERE session_token IS NOT NULL")
        conn.commit()
        return cur.rowcount


def log_impersonation_start(admin_id, target_user_id):
    """Record the start of an impersonation session."""
    with get_db_context() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO impersonation_logs (admin_id, target_user_id) VALUES (?, ?)",
            (admin_id, target_user_id)
        )
        conn.commit()
        return cur.lastrowid


def log_impersonation_stop(log_id):
    """Record the end of an impersonation session."""
    if not log_id:
        return
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE impersonation_logs SET ended_at=? WHERE id=?",
            (now, log_id)
        )
        conn.commit()


@retry_on_busy
def get_pending_users():
    """Return all users with approval_status='pending', newest first."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE approval_status='pending' ORDER BY created_at DESC"
        ).fetchall()
    return rows


@retry_on_busy
def count_pending_users():
    """Return the number of users awaiting approval."""
    with get_db_context() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM users WHERE approval_status='pending'"
        ).fetchone()[0]
    return cnt


def approve_user(user_id, admin_id):
    """Approve a pending user's account.

    Sets approval_status to 'approved', records who approved and when.
    Returns True if the user was pending and is now approved.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE users SET approval_status='approved', "
            "approved_by=?, approved_denied_at=? "
            "WHERE id=? AND approval_status='pending'",
            (admin_id, now, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def deny_user(user_id, admin_id):
    """Deny a pending user's account.

    Sets approval_status to 'denied', records when the decision was made,
    and sets denied_at for the auto-deletion countdown.
    Returns True if the user was pending and is now denied.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE users SET approval_status='denied', "
            "denied_by=?, denied_at=?, approved_denied_at=?, denied_notified=0 "
            "WHERE id=? AND approval_status='pending'",
            (admin_id, now, now, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def cleanup_denied_users(timeout_hours=24):
    """Delete denied users whose denial is older than *timeout_hours*.

    Returns the number of users deleted.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=timeout_hours)).isoformat()
    with get_db_context() as conn:
        cur = conn.execute(
            "DELETE FROM users WHERE approval_status='denied' "
            "AND denied_at IS NOT NULL AND denied_at < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()
    return deleted


def cleanup_pending_users(timeout_hours=0):
    """Delete pending users whose signup is older than *timeout_hours*.

    Only active when *timeout_hours* > 0 (disabled by default).
    The timeout is measured from ``created_at`` so accounts that were
    created but never reviewed are cleaned up.

    Returns the number of users deleted.
    """
    if timeout_hours <= 0:
        return 0
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=timeout_hours)).isoformat()
    with get_db_context() as conn:
        cur = conn.execute(
            "DELETE FROM users WHERE approval_status='pending' "
            "AND created_at < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()
    return deleted


def set_editor_access(user_id, restricted, category_ids=None):
    """Persist category access settings for an editor.

    Args:
        user_id: The editor's user ID.
        restricted: If True the editor can only access the specified categories.
        category_ids: Iterable of category IDs to allow (ignored when restricted=False).
    """
    with get_db_context() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO editor_category_access (user_id, restricted) VALUES (?, ?)",
            (user_id, 1 if restricted else 0),
        )
        conn.execute(
            "DELETE FROM editor_allowed_categories WHERE user_id=?", (user_id,)
        )
        conn.execute(
            "INSERT OR REPLACE INTO user_category_access (user_id, access_type, restricted) "
            "VALUES (?, 'write', ?)",
            (user_id, 1 if restricted else 0),
        )
        conn.execute(
            "DELETE FROM user_allowed_categories WHERE user_id=? AND access_type='write'",
            (user_id,),
        )
        if restricted and category_ids:
            rows = [(user_id, cat_id) for cat_id in category_ids]
            conn.executemany(
                "INSERT OR IGNORE INTO editor_allowed_categories (user_id, category_id) VALUES (?, ?)",
                rows,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO user_allowed_categories (user_id, category_id, access_type) "
                "VALUES (?, ?, 'write')",
                rows,
            )
        conn.commit()


def complete_initial_setup(username, password_hash, interface_language="en"):
    """Create the first administrator and close setup in the same transaction."""
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = conn.execute("SELECT setup_done FROM site_settings WHERE id=1").fetchone()
        if not settings or settings["setup_done"] or conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise ValueError("Initial setup has already been completed.")
        uid = _gen_user_id()
        conn.execute(
            "INSERT INTO users (id, username, password, role, onboarding_required) VALUES (?, ?, ?, 'owner', 1)",
            (uid, username, password_hash),
        )
        conn.execute("UPDATE site_settings SET setup_done=1, interface_language=? WHERE id=1", (interface_language,))
        conn.commit()
    return uid
