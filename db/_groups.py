"""Group chat helpers."""

import sqlite3
import string
import secrets
from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


DELETED_MESSAGE_PLACEHOLDER = "This message was deleted."


def generate_invite_code_for_group():
    """Generate a random 8-character invite code for a group chat."""
    return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


def create_group_chat(name, creator_id, description=""):
    """Create a new group chat and add the creator as owner. Returns the group dict."""
    with get_db_context() as conn:
        invite_code = generate_invite_code_for_group()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO group_chats (name, creator_id, invite_code, description, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, creator_id, invite_code, description, now),
        )
        group_id = cur.lastrowid
        cur.execute(
            "INSERT INTO group_members (group_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
            (group_id, creator_id, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM group_chats WHERE id=?", (group_id,)).fetchone()
        return dict(row)


def get_or_create_global_chat():
    """Return the global chat (is_global=1), creating it if necessary. Returns a dict."""
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM group_chats WHERE is_global=1").fetchone()
        if row:
            return dict(row)
        now = datetime.now(timezone.utc).isoformat()
        invite_code = generate_invite_code_for_group()
        try:
            conn.execute(
                "INSERT INTO group_chats (name, creator_id, invite_code, is_global, created_at) "
                "VALUES (?, NULL, ?, 1, ?)",
                ("Global Chat", invite_code, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        row = conn.execute("SELECT * FROM group_chats WHERE is_global=1").fetchone()
        return dict(row)


@retry_on_busy
def get_group_chat(group_id):
    """Return a single group chat dict, or None."""
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM group_chats WHERE id=?", (group_id,)).fetchone()
        return dict(row) if row else None


@retry_on_busy
def get_group_chat_by_invite(invite_code):
    """Return a group chat by its invite code, or None."""
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM group_chats WHERE invite_code=?", (invite_code,)).fetchone()
        return dict(row) if row else None


# A ban keeps the group_members row (with banned=1 and the old role) so the
# ban survives and blocks rejoining.  Everything that asks "is this user in
# the group, and with which role" must therefore skip banned rows, or a
# banned member keeps reading the chat and a banned moderator keeps its
# moderation powers, including lifting its own ban.  Use
# is_group_member_banned() and get_group_banned_members() to look at bans.

@retry_on_busy
def is_group_member(group_id, user_id):
    """Return True if user is a member of the group and is not banned from it."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT 1 FROM group_members WHERE group_id=? AND user_id=? AND banned=0",
            (group_id, user_id),
        ).fetchone()
        return row is not None


@retry_on_busy
def get_group_member(group_id, user_id):
    """Return the group_members row for a non-banned member, or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT * FROM group_members WHERE group_id=? AND user_id=? AND banned=0",
            (group_id, user_id),
        ).fetchone()
        return dict(row) if row else None


@retry_on_busy
def get_group_member_role(group_id, user_id):
    """Return the role string ('owner', 'moderator', 'member'), or None if not a
    member or banned."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT role FROM group_members WHERE group_id=? AND user_id=? AND banned=0",
            (group_id, user_id),
        ).fetchone()
        return row["role"] if row else None


def add_group_member(group_id, user_id, role="member"):
    """Add a user to a group. Returns True if added, False if already a member."""
    with get_db_context() as conn:
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO group_members (group_id, user_id, role, joined_at) VALUES (?, ?, ?, ?)",
                (group_id, user_id, role, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_group_member(group_id, user_id):
    """Remove a user from a group.

    Also clears the user's profile group badge entry for this group so
    that rejoining defaults back to private visibility.
    """
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM group_members WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        )
        conn.execute(
            "DELETE FROM profile_group_badges WHERE user_id=? AND group_id=?",
            (user_id, group_id),
        )
        conn.commit()


@retry_on_busy
def get_group_members(group_id):
    """Return all non-banned members of a group with their user info."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT gm.*, u.username FROM group_members gm "
            "JOIN users u ON gm.user_id=u.id "
            "WHERE gm.group_id=? AND gm.banned=0 "
            "ORDER BY CASE gm.role WHEN 'owner' THEN 0 WHEN 'moderator' THEN 1 ELSE 2 END, gm.joined_at ASC",
            (group_id,),
        ).fetchall()
        return rows


def set_group_member_role(group_id, user_id, new_role):
    """Update a member's role in a group."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE group_members SET role=? WHERE group_id=? AND user_id=?",
            (new_role, group_id, user_id),
        )
        conn.commit()


def set_group_member_timeout(group_id, user_id, until):
    """Set a timeout on a group member. `until` is an ISO datetime string, or None to clear the timeout."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE group_members SET timed_out_until=? WHERE group_id=? AND user_id=?",
            (until, group_id, user_id),
        )
        conn.commit()


def is_group_member_timed_out(group_id, user_id):
    """Return True if the member is currently timed out.

    Also auto-clears the timeout if it has elapsed.
    """
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT timed_out_until FROM group_members WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
    if not row or not row["timed_out_until"]:
        return False
    try:
        until_str = row["timed_out_until"]
        timeout_dt = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
        if timeout_dt.tzinfo is None:
            timeout_dt = timeout_dt.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) >= timeout_dt:
            # Timeout expired: clear it in the database and return False
            set_group_member_timeout(group_id, user_id, None)
            return False
        return True
    except (ValueError, TypeError):
        return False


@retry_on_busy
def get_user_groups(user_id):
    """Return all groups a user is a member of, with last message info and unread count.

    Groups the user is banned from are left out: the list shows each
    group's latest message, which a banned user must not read.
    """
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT gc.*, gm.role AS my_role, gm.unread_count, "
            "  CASE WHEN m.is_deleted=1 THEN ? ELSE m.content END AS last_message, "
            "  m.created_at AS last_message_at, "
            "  (SELECT COUNT(*) FROM group_members WHERE group_id=gc.id AND banned=0) AS member_count "
            "FROM group_chats gc "
            "JOIN group_members gm ON gc.id=gm.group_id AND gm.user_id=? AND gm.banned=0 "
            "LEFT JOIN group_messages m ON m.id = ("
            "  SELECT id FROM group_messages WHERE group_id=gc.id ORDER BY created_at DESC LIMIT 1"
            ") "
            "ORDER BY COALESCE(m.created_at, gc.created_at) DESC",
            (DELETED_MESSAGE_PLACEHOLDER, user_id),
        ).fetchall()
        return rows


def send_group_message(group_id, sender_id, content, ip_address="", is_system=False):
    """Insert a new message into a group chat. Returns the new message id."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO group_messages (group_id, sender_id, content, is_system, ip_address, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, sender_id, content, 1 if is_system else 0, ip_address, now),
        )
        msg_id = cur.lastrowid
        conn.commit()
        return msg_id


def send_group_system_message(group_id, content):
    """Insert a system message (no sender) into a group chat."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO group_messages (group_id, sender_id, content, is_system, ip_address, created_at) "
            "VALUES (?, NULL, ?, 1, '', ?)",
            (group_id, content, now),
        )
        msg_id = cur.lastrowid
        conn.commit()
        return msg_id


@retry_on_busy
def get_group_messages(group_id):
    """Return all messages in a group, oldest first, with sender info and attachments."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT gm.*, u.username AS sender_name "
            "FROM group_messages gm "
            "LEFT JOIN users u ON gm.sender_id=u.id "
            "WHERE gm.group_id=? "
            "ORDER BY gm.created_at ASC",
            (group_id,),
        ).fetchall()
        messages = []
        for r in rows:
            msg = dict(r)
            atts = conn.execute(
                "SELECT * FROM group_attachments WHERE message_id=? ORDER BY id ASC",
                (r["id"],),
            ).fetchall()
            msg["attachments"] = [dict(a) for a in atts]
            messages.append(msg)
        return messages


@retry_on_busy
def get_all_messages_for_groups(group_ids):
    """Return all messages for the given *group_ids* in a single query.

    Returns a dict mapping group_id -> list of message dicts (oldest first).
    This avoids the N+1 pattern of calling ``get_group_messages`` per group.
    """
    if not group_ids:
        return {}
    result = {gid: [] for gid in group_ids}
    with get_db_context() as conn:
        placeholders = ",".join("?" for _ in group_ids)
        rows = conn.execute(
            "SELECT gm.*, u.username AS sender_name "
            "FROM group_messages gm "
            "LEFT JOIN users u ON gm.sender_id=u.id "
            f"WHERE gm.group_id IN ({placeholders}) "  # noqa: S608
            "ORDER BY gm.group_id, gm.created_at ASC",
            tuple(group_ids),
        ).fetchall()
        for r in rows:
            result[r["group_id"]].append(dict(r))
    return result


def add_group_attachment(message_id, filename, original_name, file_size, blob_id=None):
    """Record a new attachment for a group message. Returns the attachment id."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO group_attachments (message_id, filename, original_name, file_size, created_at, blob_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, filename, original_name, file_size, now, blob_id),
        )
        att_id = cur.lastrowid
        conn.commit()
        return att_id


@retry_on_busy
def get_group_attachment(attachment_id):
    """Return a single group attachment row with group_id and sender_id, or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT ga.*, gm.group_id, gm.sender_id, gm.is_deleted "
            "FROM group_attachments ga "
            "JOIN group_messages gm ON ga.message_id=gm.id "
            "WHERE ga.id=?",
            (attachment_id,),
        ).fetchone()
        return dict(row) if row else None


def delete_group_message(message_id):
    """Soft-delete a group message while preserving content and attachments.

    Deleted attachments stay linked to the message so admins can continue to
    review them, and they are still removed by the existing clear/export/age-
    based cleanup flows when those run.
    """
    with get_db_context() as conn:
        conn.execute(
            "UPDATE group_messages SET is_deleted=1, deleted_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), message_id),
        )
        conn.commit()


@retry_on_busy
def get_group_message_by_id(message_id):
    """Return a single group message row, or None."""
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM group_messages WHERE id=?", (message_id,)).fetchone()
        return dict(row) if row else None


@retry_on_busy
def get_all_group_chats_admin():
    """Return all group chats with member counts (for admin view)."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT gc.*, u.username AS creator_name, "
            "  (SELECT COUNT(*) FROM group_members WHERE group_id=gc.id) AS member_count, "
            "  (SELECT COUNT(*) FROM group_messages WHERE group_id=gc.id) AS message_count, "
            "  m.created_at AS last_message_at "
            "FROM group_chats gc "
            "LEFT JOIN users u ON gc.creator_id=u.id "
            "LEFT JOIN group_messages m ON m.id = ("
            "  SELECT id FROM group_messages WHERE group_id=gc.id ORDER BY created_at DESC LIMIT 1"
            ") "
            "ORDER BY COALESCE(m.created_at, gc.created_at) DESC"
        ).fetchall()
        return rows


@retry_on_busy
def get_all_group_messages_for_backup():
    """Return all group messages with sender info and attachments for Telegram backup."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT gm.*, gc.name AS group_name, u.username AS sender_name "
            "FROM group_messages gm "
            "JOIN group_chats gc ON gm.group_id=gc.id "
            "LEFT JOIN users u ON gm.sender_id=u.id "
            "ORDER BY gm.created_at ASC"
        ).fetchall()
        messages = []
        for r in rows:
            msg = dict(r)
            atts = conn.execute(
                "SELECT * FROM group_attachments WHERE message_id=? ORDER BY id ASC",
                (r["id"],),
            ).fetchall()
            msg["attachments"] = [dict(a) for a in atts]
            messages.append(msg)
        return messages


@retry_on_busy
def get_group_messages_for_export(group_id):
    """Return all messages for a specific group with sender info and attachments for export.

    Args:
        group_id: The group chat ID to export messages from.

    Returns:
        A tuple of (messages, group_info) where:
        - messages is a list of message dicts with attachments
        - group_info is the group chat dict
    """
    with get_db_context() as conn:
        # Get group info
        group_row = conn.execute(
            "SELECT * FROM group_chats WHERE id=?", (group_id,)
        ).fetchone()
        if not group_row:
            return [], None

        group_info = dict(group_row)

        # Get all messages for this group
        rows = conn.execute(
            "SELECT gm.*, u.username AS sender_name "
            "FROM group_messages gm "
            "LEFT JOIN users u ON gm.sender_id=u.id "
            "WHERE gm.group_id=? "
            "ORDER BY gm.created_at ASC",
            (group_id,)
        ).fetchall()

        messages = []
        for r in rows:
            msg = dict(r)
            # Get attachments for this message
            atts = conn.execute(
                "SELECT * FROM group_attachments WHERE message_id=? ORDER BY id ASC",
                (r["id"],),
            ).fetchall()
            msg["attachments"] = [dict(a) for a in atts]
            messages.append(msg)

        return messages, group_info


def cleanup_old_group_messages(retention_days=30):
    """Delete group messages older than retention_days (and their attachments via CASCADE).

    Args:
        retention_days: Keep messages newer than this many days

    Returns list of attachment filenames that need to be removed from disk.
    Unlike DM chats, group chats themselves are NOT deleted (they persist).
    """
    with get_db_context() as conn:
        # Collect attachment filenames for messages that will be deleted
        filenames = conn.execute(
            """SELECT ga.filename FROM group_attachments ga
               JOIN group_messages gm ON ga.message_id = gm.id
               WHERE datetime(gm.created_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,)
        ).fetchall()
        files_to_delete = [r["filename"] for r in filenames]
        # Delete messages older than retention period (attachments cascade)
        conn.execute(
            """DELETE FROM group_messages
               WHERE datetime(created_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,)
        )
        conn.commit()
        return files_to_delete


def cleanup_old_group_attachments(retention_days=7):
    """Delete only group attachments older than retention_days (keep messages).

    Args:
        retention_days: Keep attachments newer than this many days

    Returns list of attachment filenames that need to be removed from disk.
    """
    with get_db_context() as conn:
        # Collect attachment filenames that will be deleted
        filenames = conn.execute(
            """SELECT filename FROM group_attachments
               WHERE datetime(created_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,)
        ).fetchall()
        files_to_delete = [r["filename"] for r in filenames]
        # Delete attachments older than retention period
        conn.execute(
            """DELETE FROM group_attachments
               WHERE datetime(created_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,)
        )
        conn.commit()
        return files_to_delete


def clear_group_messages(group_id):
    """Delete all messages and attachments in a specific group.

    Returns list of attachment filenames that need to be removed from disk.
    """
    with get_db_context() as conn:
        # Collect attachment filenames for messages that will be deleted
        filenames = conn.execute(
            """SELECT ga.filename FROM group_attachments ga
               JOIN group_messages gm ON ga.message_id = gm.id
               WHERE gm.group_id = ?""",
            (group_id,)
        ).fetchall()
        files_to_delete = [r["filename"] for r in filenames]
        # Delete all messages in this group (attachments cascade)
        conn.execute("DELETE FROM group_messages WHERE group_id = ?", (group_id,))
        # Reset unread counts for all members
        conn.execute(
            "UPDATE group_members SET unread_count = 0 WHERE group_id = ?",
            (group_id,)
        )
        conn.commit()
        return files_to_delete


def increment_group_unread_count(group_id, for_user_id):
    """Increment the unread count for a specific user in a group."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE group_members SET unread_count = unread_count + 1 WHERE group_id = ? AND user_id = ?",
            (group_id, for_user_id)
        )
        conn.commit()


def bulk_increment_group_unread_count(group_id, exclude_user_id):
    """Increment the unread count for all group members except the sender in one query.

    Replaces an N+1 loop (one UPDATE per member) with a single batch UPDATE,
    eliminating the O(N) round-trips to SQLite when a message is posted to a group.
    """
    with get_db_context() as conn:
        conn.execute(
            "UPDATE group_members SET unread_count = unread_count + 1 "
            "WHERE group_id = ? AND user_id != ? AND banned = 0",
            (group_id, exclude_user_id),
        )
        conn.commit()


def reset_group_unread_count(group_id, for_user_id):
    """Reset the unread count for a specific user in a group."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE group_members SET unread_count = 0 WHERE group_id = ? AND user_id = ?",
            (group_id, for_user_id)
        )
        conn.commit()


@retry_on_busy
def get_total_unread_group_count(user_id):
    """Get the total number of unread group messages for a user."""
    with get_db_context() as conn:
        count = conn.execute(
            "SELECT COALESCE(SUM(unread_count), 0) AS total FROM group_members "
            "WHERE user_id = ? AND banned = 0",
            (user_id,)
        ).fetchone()
        return count["total"] if count else 0


@retry_on_busy
def get_user_group_attachment_count_today(user_id):
    """Return the number of group attachments sent by user today (UTC)."""
    with get_db_context() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM group_attachments ga "
            "JOIN group_messages gm ON ga.message_id=gm.id "
            "WHERE gm.sender_id=? AND DATE(gm.created_at)=DATE('now')",
            (user_id,),
        ).fetchone()[0]
        return count


def transfer_group_ownership(group_id, old_owner_id, new_owner_id):
    """Transfer group ownership: old owner becomes moderator, new owner gets ownership."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE group_members SET role='moderator' WHERE group_id=? AND user_id=?",
            (group_id, old_owner_id),
        )
        conn.execute(
            "UPDATE group_members SET role='owner' WHERE group_id=? AND user_id=?",
            (group_id, new_owner_id),
        )
        conn.execute(
            "UPDATE group_chats SET creator_id=? WHERE id=?",
            (new_owner_id, group_id),
        )
        conn.commit()


def ban_group_member(group_id, user_id):
    """Permanently ban a user from a group. The member row is kept with banned=1."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE group_members SET banned=1 WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        )
        conn.commit()


def unban_group_member(group_id, user_id):
    """Revoke a ban and remove the user from the group so they can rejoin."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM group_members WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        )
        conn.commit()


@retry_on_busy
def is_group_member_banned(group_id, user_id):
    """Return True if the user is banned from the group."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT banned FROM group_members WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
    if not row:
        return False
    return bool(row["banned"])


@retry_on_busy
def get_group_banned_members(group_id):
    """Return all banned members of a group with their user info."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT gm.*, u.username FROM group_members gm "
            "JOIN users u ON gm.user_id=u.id "
            "WHERE gm.group_id=? AND gm.banned=1 "
            "ORDER BY gm.joined_at ASC",
            (group_id,),
        ).fetchall()
        return rows


def regenerate_group_invite_code(group_id, custom_code=None):
    """Generate a new invite code for a group, revoking the previous one. Returns the new code.

    If *custom_code* is provided it is used directly (caller must validate format/uniqueness).
    Otherwise a random 8-character code is generated.
    """
    with get_db_context() as conn:
        new_code = custom_code if custom_code else generate_invite_code_for_group()
        conn.execute(
            "UPDATE group_chats SET invite_code=? WHERE id=?",
            (new_code, group_id),
        )
        conn.commit()
        return new_code


def set_group_chat_active(group_id, is_active):
    """Set the active/inactive state of a group chat (used to deactivate/reactivate the global chat)."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE group_chats SET is_active=? WHERE id=?",
            (1 if is_active else 0, group_id),
        )
        conn.commit()


def delete_group_chat(group_id):
    """Permanently delete a group chat and all its messages/members.

    Returns a list of attachment filenames that need to be removed from disk.
    The global chat cannot be deleted via this function. Callers must enforce that.
    """
    with get_db_context() as conn:
        filenames = conn.execute(
            "SELECT ga.filename FROM group_attachments ga "
            "JOIN group_messages gm ON ga.message_id=gm.id "
            "WHERE gm.group_id=?",
            (group_id,),
        ).fetchall()
        files = [r["filename"] for r in filenames]
        # Cascade deletes messages, members, attachments via FK constraints
        conn.execute("DELETE FROM group_chats WHERE id=?", (group_id,))
        conn.commit()
        return files
