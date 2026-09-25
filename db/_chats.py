"""Direct messaging (DM) chat helpers."""

import sqlite3
from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


DELETED_MESSAGE_PLACEHOLDER = "This message was deleted."


def get_or_create_chat(user1_id, user2_id):
    """Return an existing chat between two users, or create one.

    The pair is always stored with the smaller id first to enforce
    the UNIQUE constraint regardless of who initiates.
    """
    a, b = sorted([user1_id, user2_id])
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT * FROM chats WHERE user1_id=? AND user2_id=?", (a, b)
        ).fetchone()
        if row:
            return dict(row)
        try:
            conn.execute(
                "INSERT INTO chats (user1_id, user2_id) VALUES (?, ?)", (a, b)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # concurrent insert: row already exists
        row = conn.execute(
            "SELECT * FROM chats WHERE user1_id=? AND user2_id=?", (a, b)
        ).fetchone()
        return dict(row)


@retry_on_busy
def get_chat_by_id(chat_id):
    """Return a single chat row or None."""
    with get_db_context() as conn:
        return conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()


@retry_on_busy
def is_chat_participant(chat_id, user_id):
    """Return True if user_id is part of the chat."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT 1 FROM chats WHERE id=? AND (user1_id=? OR user2_id=?)",
            (chat_id, user_id, user_id),
        ).fetchone()
        return row is not None


@retry_on_busy
def get_user_chats(user_id):
    """Return all chats for a user, with the other user's info, last message, and unread count."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "  CASE WHEN c.user1_id=? THEN u2.username ELSE u1.username END AS other_username, "
            "  CASE WHEN c.user1_id=? THEN c.user2_id ELSE c.user1_id END AS other_user_id, "
            "  CASE WHEN c.user1_id=? THEN c.unread_count_user1 ELSE c.unread_count_user2 END AS unread_count, "
            "  CASE WHEN m.is_deleted=1 THEN ? ELSE m.content END AS last_message, "
            "  m.created_at AS last_message_at "
            "FROM chats c "
            "JOIN users u1 ON c.user1_id=u1.id "
            "JOIN users u2 ON c.user2_id=u2.id "
            "LEFT JOIN chat_messages m ON m.id = ("
            "  SELECT id FROM chat_messages WHERE chat_id=c.id ORDER BY created_at DESC LIMIT 1"
            ") "
            "WHERE c.user1_id=? OR c.user2_id=? "
            "ORDER BY COALESCE(m.created_at, c.created_at) DESC",
            (user_id, user_id, user_id, DELETED_MESSAGE_PLACEHOLDER, user_id, user_id),
        ).fetchall()
        return rows


@retry_on_busy
def get_chat_messages(chat_id):
    """Return all messages in a chat, oldest first, with sender info and attachments."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT cm.*, u.username AS sender_name "
            "FROM chat_messages cm "
            "JOIN users u ON cm.sender_id=u.id "
            "WHERE cm.chat_id=? "
            "ORDER BY cm.created_at ASC",
            (chat_id,),
        ).fetchall()
        # Fetch attachments for each message
        messages = []
        for r in rows:
            msg = dict(r)
            atts = conn.execute(
                "SELECT * FROM chat_attachments WHERE message_id=? ORDER BY id ASC",
                (r["id"],),
            ).fetchall()
            msg["attachments"] = [dict(a) for a in atts]
            messages.append(msg)
        return messages


@retry_on_busy
def get_chat_message_by_id(message_id):
    """Return a single chat message row, or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT cm.*, u.username AS sender_name "
            "FROM chat_messages cm "
            "JOIN users u ON cm.sender_id=u.id "
            "WHERE cm.id=?",
            (message_id,),
        ).fetchone()
        if not row:
            return None
        msg = dict(row)
        atts = conn.execute(
            "SELECT * FROM chat_attachments WHERE message_id=? ORDER BY id ASC",
            (message_id,),
        ).fetchall()
        msg["attachments"] = [dict(a) for a in atts]
        return msg


@retry_on_busy
def get_all_messages_for_chats(chat_ids):
    """Return all messages for the given *chat_ids* in a single query.

    Returns a dict mapping chat_id -> list of message dicts (oldest first).
    This avoids the N+1 pattern of calling ``get_chat_messages`` per chat.
    """
    if not chat_ids:
        return {}
    result = {cid: [] for cid in chat_ids}
    with get_db_context() as conn:
        placeholders = ",".join("?" for _ in chat_ids)
        rows = conn.execute(
            "SELECT cm.*, u.username AS sender_name "
            "FROM chat_messages cm "
            "JOIN users u ON cm.sender_id=u.id "
            f"WHERE cm.chat_id IN ({placeholders}) "  # noqa: S608
            "ORDER BY cm.chat_id, cm.created_at ASC",
            tuple(chat_ids),
        ).fetchall()
        for r in rows:
            result[r["chat_id"]].append(dict(r))
    return result


def send_chat_message(chat_id, sender_id, content, ip_address=""):
    """Insert a new message into a chat. Returns the new message id."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (chat_id, sender_id, content, ip_address, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, sender_id, content, ip_address, now),
        )
        msg_id = cur.lastrowid
        conn.commit()
        return msg_id


def add_chat_attachment(message_id, filename, original_name, file_size, blob_id=None):
    """Record a new attachment for a chat message. Returns the attachment id."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_attachments (message_id, filename, original_name, file_size, created_at, blob_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, filename, original_name, file_size, now, blob_id),
        )
        att_id = cur.lastrowid
        conn.commit()
        return att_id


@retry_on_busy
def get_user_chat_attachment_count_today(user_id):
    """Return the number of chat attachments sent by user today (UTC)."""
    with get_db_context() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM chat_attachments ca "
            "JOIN chat_messages cm ON ca.message_id=cm.id "
            "WHERE cm.sender_id=? AND DATE(cm.created_at)=DATE('now')",
            (user_id,),
        ).fetchone()[0]
        return count


@retry_on_busy
def get_chat_attachment(attachment_id):
    """Return a single chat attachment row or None."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT ca.*, cm.chat_id, cm.sender_id, cm.is_deleted "
            "FROM chat_attachments ca "
            "JOIN chat_messages cm ON ca.message_id=cm.id "
            "WHERE ca.id=?",
            (attachment_id,),
        ).fetchone()


@retry_on_busy
def get_all_chats_admin():
    """Return all chats with both usernames (for admin view)."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT c.*, u1.username AS user1_name, u2.username AS user2_name, "
            "  (SELECT COUNT(*) FROM chat_messages WHERE chat_id=c.id) AS message_count, "
            "  m.created_at AS last_message_at "
            "FROM chats c "
            "JOIN users u1 ON c.user1_id=u1.id "
            "JOIN users u2 ON c.user2_id=u2.id "
            "LEFT JOIN chat_messages m ON m.id = ("
            "  SELECT id FROM chat_messages WHERE chat_id=c.id ORDER BY created_at DESC LIMIT 1"
            ") "
            "ORDER BY COALESCE(m.created_at, c.created_at) DESC"
        ).fetchall()


@retry_on_busy
def get_user_chats_admin(user_id):
    """Return all chats for a specific user (admin view)."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT c.*, u1.username AS user1_name, u2.username AS user2_name, "
            "  (SELECT COUNT(*) FROM chat_messages WHERE chat_id=c.id) AS message_count, "
            "  m.created_at AS last_message_at "
            "FROM chats c "
            "JOIN users u1 ON c.user1_id=u1.id "
            "JOIN users u2 ON c.user2_id=u2.id "
            "LEFT JOIN chat_messages m ON m.id = ("
            "  SELECT id FROM chat_messages WHERE chat_id=c.id ORDER BY created_at DESC LIMIT 1"
            ") "
            "WHERE c.user1_id=? OR c.user2_id=? "
            "ORDER BY COALESCE(m.created_at, c.created_at) DESC",
            (user_id, user_id),
        ).fetchall()


@retry_on_busy
def get_all_messages_for_backup():
    """Return all chat messages with sender/receiver info and attachments for Telegram backup."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT cm.*, u.username AS sender_name, "
            "  CASE WHEN c.user1_id=cm.sender_id THEN u2.username ELSE u1.username END AS receiver_name "
            "FROM chat_messages cm "
            "JOIN chats c ON cm.chat_id=c.id "
            "JOIN users u ON cm.sender_id=u.id "
            "JOIN users u1 ON c.user1_id=u1.id "
            "JOIN users u2 ON c.user2_id=u2.id "
            "ORDER BY cm.created_at ASC"
        ).fetchall()
        messages = []
        for r in rows:
            msg = dict(r)
            atts = conn.execute(
                "SELECT * FROM chat_attachments WHERE message_id=? ORDER BY id ASC",
                (r["id"],),
            ).fetchall()
            msg["attachments"] = [dict(a) for a in atts]
            messages.append(msg)
        return messages


def cleanup_old_chat_messages(retention_days=30):
    """Delete chat messages older than retention_days (and their attachments via CASCADE).

    Args:
        retention_days: Keep messages newer than this many days

    Returns list of attachment filenames that need to be removed from disk.
    """
    with get_db_context() as conn:
        # Collect attachment filenames for messages that will be deleted
        filenames = conn.execute(
            """SELECT ca.filename FROM chat_attachments ca
               JOIN chat_messages cm ON ca.message_id = cm.id
               WHERE datetime(cm.created_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,)
        ).fetchall()
        files_to_delete = [r["filename"] for r in filenames]
        # Delete messages older than retention period (attachments cascade)
        conn.execute(
            """DELETE FROM chat_messages
               WHERE datetime(created_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,)
        )
        # Also delete empty chats (chats with no messages)
        conn.execute(
            "DELETE FROM chats WHERE id NOT IN (SELECT DISTINCT chat_id FROM chat_messages)"
        )
        conn.commit()
        return files_to_delete


def cleanup_old_chat_attachments(retention_days=7):
    """Delete only chat attachments older than retention_days (keep messages).

    Args:
        retention_days: Keep attachments newer than this many days

    Returns list of attachment filenames that need to be removed from disk.
    """
    with get_db_context() as conn:
        # Collect attachment filenames that will be deleted
        filenames = conn.execute(
            """SELECT filename FROM chat_attachments
               WHERE datetime(created_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,)
        ).fetchall()
        files_to_delete = [r["filename"] for r in filenames]
        # Delete attachments older than retention period
        conn.execute(
            """DELETE FROM chat_attachments
               WHERE datetime(created_at) < datetime('now', '-' || ? || ' days')""",
            (retention_days,)
        )
        conn.commit()
        return files_to_delete


def clear_chat_messages(chat_id):
    """Delete all messages and attachments in a specific chat.

    Returns list of attachment filenames that need to be removed from disk.
    """
    with get_db_context() as conn:
        # Collect attachment filenames for messages that will be deleted
        filenames = conn.execute(
            """SELECT ca.filename FROM chat_attachments ca
               JOIN chat_messages cm ON ca.message_id = cm.id
               WHERE cm.chat_id = ?""",
            (chat_id,)
        ).fetchall()
        files_to_delete = [r["filename"] for r in filenames]
        # Delete all messages in this chat (attachments cascade)
        conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        # Reset unread counts
        conn.execute(
            "UPDATE chats SET unread_count_user1 = 0, unread_count_user2 = 0 WHERE id = ?",
            (chat_id,)
        )
        conn.commit()
        return files_to_delete


def delete_chat_message(message_id):
    """Soft-delete a chat message while preserving content and attachments.

    Deleted attachments stay linked to the message so admins can continue to
    review them, and they are still removed by the existing clear/export/age-
    based cleanup flows when those run.
    """
    with get_db_context() as conn:
        conn.execute(
            "UPDATE chat_messages SET is_deleted=1, deleted_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), message_id),
        )
        conn.commit()


def increment_unread_count(chat_id, for_user_id):
    """Increment the unread count for a specific user in a chat."""
    with get_db_context() as conn:
        chat = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if not chat:
            return

        # Determine which unread counter to increment
        if chat["user1_id"] == for_user_id:
            conn.execute("UPDATE chats SET unread_count_user1 = unread_count_user1 + 1 WHERE id = ?", (chat_id,))
        elif chat["user2_id"] == for_user_id:
            conn.execute("UPDATE chats SET unread_count_user2 = unread_count_user2 + 1 WHERE id = ?", (chat_id,))

        conn.commit()


def reset_unread_count(chat_id, for_user_id):
    """Reset the unread count for a specific user in a chat."""
    with get_db_context() as conn:
        chat = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if not chat:
            return

        # Determine which unread counter to reset
        if chat["user1_id"] == for_user_id:
            conn.execute("UPDATE chats SET unread_count_user1 = 0 WHERE id = ?", (chat_id,))
        elif chat["user2_id"] == for_user_id:
            conn.execute("UPDATE chats SET unread_count_user2 = 0 WHERE id = ?", (chat_id,))

        conn.commit()


@retry_on_busy
def get_total_unread_dm_count(user_id):
    """Get the total number of unread direct messages for a user."""
    with get_db_context() as conn:
        count = conn.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN user1_id = ? THEN unread_count_user1 ELSE 0 END), 0) +
                COALESCE(SUM(CASE WHEN user2_id = ? THEN unread_count_user2 ELSE 0 END), 0) AS total
               FROM chats
               WHERE user1_id = ? OR user2_id = ?""",
            (user_id, user_id, user_id, user_id)
        ).fetchone()
        return count["total"] if count else 0
