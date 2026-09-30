"""Direct message conversations (``chats``): one row per pair of users.

The pair is stored with the smaller user id first (``UNIQUE(user1_id,
user2_id)``), and each side has its own unread counter.
"""

from __future__ import annotations

from typing import Any

from ... import storage
from ...db import db
from . import store
from .store import DM


def get(chat_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM chats WHERE id = ?", (chat_id,))


def is_participant(chat: dict[str, Any], user_id: str) -> bool:
    return user_id in (chat["user1_id"], chat["user2_id"])


def other_id(chat: dict[str, Any], user_id: str) -> str:
    return chat["user2_id"] if chat["user1_id"] == user_id else chat["user1_id"]


def _unread_column(chat: dict[str, Any], user_id: str) -> str:
    return "unread_count_user1" if chat["user1_id"] == user_id else "unread_count_user2"


def get_or_create(user_id: str, other_user_id: str) -> dict[str, Any]:
    first, second = sorted((user_id, other_user_id))
    db.execute("INSERT OR IGNORE INTO chats (user1_id, user2_id) VALUES (?, ?)", (first, second))
    return db.one("SELECT * FROM chats WHERE user1_id = ? AND user2_id = ?", (first, second))  # type: ignore[return-value]


def list_for(user_id: str) -> list[dict[str, Any]]:
    """The user's conversations, most recent activity first."""
    return db.all(
        "SELECT c.id, c.created_at, u.id AS other_user_id, u.username AS other_username, "
        "CASE WHEN c.user1_id = ? THEN c.unread_count_user1 ELSE c.unread_count_user2 END AS unread_count, "
        "m.content AS last_message, m.is_deleted AS last_deleted, m.created_at AS last_message_at "
        "FROM chats c "
        "JOIN users u ON u.id = CASE WHEN c.user1_id = ? THEN c.user2_id ELSE c.user1_id END "
        "LEFT JOIN chat_messages m ON m.id = (SELECT MAX(id) FROM chat_messages WHERE chat_id = c.id) "
        "WHERE c.user1_id = ? OR c.user2_id = ? "
        "ORDER BY COALESCE(m.created_at, c.created_at) DESC, c.id DESC",
        (user_id, user_id, user_id, user_id),
    )


def unread_total(user_id: str) -> int:
    return int(db.scalar(
        "SELECT COALESCE(SUM(CASE WHEN user1_id = ? THEN unread_count_user1 ELSE unread_count_user2 END), 0) "
        "FROM chats WHERE user1_id = ? OR user2_id = ?",
        (user_id, user_id, user_id),
        default=0,
    ) or 0)


def unread_for(chat: dict[str, Any], user_id: str) -> int:
    return int(chat[_unread_column(chat, user_id)] or 0)


def mark_read(chat: dict[str, Any], user_id: str) -> None:
    """Reset the user's unread counter; writes nothing when it is already zero."""
    column = _unread_column(chat, user_id)
    db.execute(f"UPDATE chats SET {column} = 0 WHERE id = ? AND {column} > 0", (chat["id"],))


def send(chat: dict[str, Any], sender_id: str, content: str, *, ip_address: str,
         stored: storage.StoredFile | None) -> int:
    other_column = _unread_column(chat, other_id(chat, sender_id))
    with db.transaction():
        message_id = store.insert(DM, chat["id"], sender_id, content, ip_address=ip_address, stored=stored)
        db.execute(f"UPDATE chats SET {other_column} = {other_column} + 1 WHERE id = ?", (chat["id"],))
    return message_id


def clear(chat: dict[str, Any]) -> None:
    with db.transaction():
        files = store.clear(DM, chat["id"])
        db.execute("UPDATE chats SET unread_count_user1 = 0, unread_count_user2 = 0 WHERE id = ?", (chat["id"],))
    store.remove_files(files)


def admin_page(*, user_id: str | None, page: int, per_page: int) -> tuple[list[dict[str, Any]], int]:
    """Conversations for the monitoring page (optionally of one user) and the total count."""
    where, params = ("WHERE c.user1_id = ? OR c.user2_id = ?", [user_id, user_id]) if user_id else ("", [])
    total = int(db.scalar(f"SELECT COUNT(*) FROM chats c {where}", params, default=0) or 0)
    rows = db.all(
        "SELECT c.id, c.created_at, u1.username AS user1_name, u2.username AS user2_name, "
        "(SELECT COUNT(*) FROM chat_messages WHERE chat_id = c.id) AS message_count, "
        "m.created_at AS last_message_at "
        "FROM chats c LEFT JOIN users u1 ON u1.id = c.user1_id LEFT JOIN users u2 ON u2.id = c.user2_id "
        "LEFT JOIN chat_messages m ON m.id = (SELECT MAX(id) FROM chat_messages WHERE chat_id = c.id) "
        f"{where} ORDER BY COALESCE(m.created_at, c.created_at) DESC, c.id DESC LIMIT ? OFFSET ?",
        [*params, per_page, (page - 1) * per_page],
    )
    return rows, total


def delete_empty_before(cutoff: str) -> int:
    """Retention: drop conversations without messages that were started before *cutoff*."""
    return db.execute(
        "DELETE FROM chats WHERE created_at < ? AND NOT EXISTS (SELECT 1 FROM chat_messages WHERE chat_id = chats.id)",
        (cutoff,),
    ).rowcount
