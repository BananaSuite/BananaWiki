"""Direct message conversations (``chats``): one row per pair of users.

The pair is stored with the smaller user id first (``UNIQUE(user1_id,
user2_id)``), and each side has its own unread counter.

Clearing a conversation hides it from the participant who clears it only
(``cleared_upto_user1`` / ``cleared_upto_user2``, the newest message they
cleared): the other keeps their copy, so nobody can erase what the other
side received. Messages both participants have cleared are erased for good.
An administrator moderating a conversation can still clear it for both
(:func:`clear`).

A sender may delete a message for both only within
:data:`DELETE_WINDOW_MINUTES` of sending it, to take back a mistake; after
that it belongs to the recipient's copy too. Deleting an account is the
exception: ``chats`` and ``chat_messages`` go with either participant's
account (``ON DELETE CASCADE``), so the conversation disappears for both;
reported messages keep their copy in ``chat__reports``.

A message sent to someone who blocked the sender is withheld: stored for its
sender (who is not told) and the administrators, never delivered to the
recipient, also after an unblock (see :mod:`.blocks`).
"""

from __future__ import annotations

from typing import Any

from ....core.timeutil import sql_in
from ... import storage
from ...db import db
from . import blocks, store
from .store import DM

DELETE_WINDOW_MINUTES = 15


class SendRefused(ValueError):
    """A block between the participants (see :mod:`.blocks`); ``key`` is the translation key."""

    def __init__(self, key: str, status: int = 403):
        super().__init__(key)
        self.key = key
        self.status = status


def get(chat_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM chats WHERE id = ?", (chat_id,))


def is_participant(chat: dict[str, Any], user_id: str) -> bool:
    return user_id in (chat["user1_id"], chat["user2_id"])


def other_id(chat: dict[str, Any], user_id: str) -> str:
    return chat["user2_id"] if chat["user1_id"] == user_id else chat["user1_id"]


def _unread_column(chat: dict[str, Any], user_id: str) -> str:
    return "unread_count_user1" if chat["user1_id"] == user_id else "unread_count_user2"


def _cleared_column(chat: dict[str, Any], user_id: str) -> str:
    return "cleared_upto_user1" if chat["user1_id"] == user_id else "cleared_upto_user2"


def cleared_upto(chat: dict[str, Any], user_id: str) -> int:
    """The newest message *user_id* cleared from their view (0: none); older ones are hidden from them."""
    return int(chat.get(_cleared_column(chat, user_id)) or 0)


def deletable_since() -> str:
    """Senders may delete their messages sent at or after this time (see :data:`DELETE_WINDOW_MINUTES`)."""
    return sql_in(minutes=-DELETE_WINDOW_MINUTES)


def deletable(message: dict[str, Any]) -> bool:
    """Whether its sender may still delete *message* (a row with ``created_at``) for both participants."""
    return (message.get("created_at") or "") >= deletable_since()


def find(user_id: str, other_user_id: str) -> dict[str, Any] | None:
    """The conversation of the two users, if they have one."""
    first, second = sorted((user_id, other_user_id))
    return db.one("SELECT * FROM chats WHERE user1_id = ? AND user2_id = ?", (first, second))


def get_or_create(user_id: str, other_user_id: str) -> dict[str, Any]:
    first, second = sorted((user_id, other_user_id))
    db.execute("INSERT OR IGNORE INTO chats (user1_id, user2_id) VALUES (?, ?)", (first, second))
    return db.one("SELECT * FROM chats WHERE user1_id = ? AND user2_id = ?", (first, second))  # type: ignore[return-value]


def list_for(user_id: str) -> list[dict[str, Any]]:
    """The user's conversations, most recent activity first (messages withheld from them do not count)."""
    return db.all(
        "SELECT c.id, c.created_at, u.id AS other_user_id, u.username AS other_username, "
        "CASE WHEN c.user1_id = ? THEN c.unread_count_user1 ELSE c.unread_count_user2 END AS unread_count, "
        "m.content AS last_message, m.is_deleted AS last_deleted, m.created_at AS last_message_at "
        "FROM chats c "
        "JOIN users u ON u.id = CASE WHEN c.user1_id = ? THEN c.user2_id ELSE c.user1_id END "
        "LEFT JOIN chat_messages m ON m.id = (SELECT MAX(id) FROM chat_messages WHERE chat_id = c.id AND id > "
        "CASE WHEN c.user1_id = ? THEN c.cleared_upto_user1 ELSE c.cleared_upto_user2 END "
        "AND (withheld = 0 OR sender_id = ?)) "
        "WHERE c.user1_id = ? OR c.user2_id = ? "
        "ORDER BY COALESCE(m.created_at, c.created_at) DESC, c.id DESC",
        (user_id, user_id, user_id, user_id, user_id, user_id),
    )


def unread_total(user_id: str) -> int:
    return int(db.scalar(
        "SELECT COALESCE(SUM(CASE WHEN user1_id = ? THEN unread_count_user1 ELSE unread_count_user2 END), 0) "
        "FROM chats WHERE user1_id = ? OR user2_id = ?",
        (user_id, user_id, user_id),
        default=0,
    ) or 0)


def mark_read(chat: dict[str, Any], user_id: str) -> None:
    """Reset the user's unread counter; writes nothing when it is already zero."""
    column = _unread_column(chat, user_id)
    db.execute(f"UPDATE chats SET {column} = 0 WHERE id = ? AND {column} > 0", (chat["id"],))


def send(chat: dict[str, Any], sender_id: str, content: str, *, ip_address: str,
         stored: storage.StoredFile | None) -> int:
    """Add a message; raises :class:`SendRefused` when the sender blocked the recipient.

    When the recipient blocked the sender, the message is withheld from them
    (and is not unread for them); the sender sees no difference.
    """
    recipient = other_id(chat, sender_id)
    other_column = _unread_column(chat, recipient)
    with db.transaction():
        refusal = blocks.dm_refusal(sender_id, recipient)
        if refusal:
            raise SendRefused(refusal)
        withheld = blocks.blocked(recipient, sender_id)
        message_id = store.insert(DM, chat["id"], sender_id, content, ip_address=ip_address, stored=stored,
                                  withheld=withheld)
        if not withheld:
            db.execute(f"UPDATE chats SET {other_column} = {other_column} + 1 WHERE id = ?", (chat["id"],))
    return message_id


def clear(chat: dict[str, Any]) -> None:
    """Erase the whole conversation for both participants (administrators moderating)."""
    with db.transaction():
        files = store.clear(DM, chat["id"])
        db.execute("UPDATE chats SET unread_count_user1 = 0, unread_count_user2 = 0 WHERE id = ?", (chat["id"],))
    store.remove_files(files)


def clear_for(chat: dict[str, Any], user_id: str) -> tuple[int, int]:
    """Hide the conversation so far from *user_id*; return the (messages, attachments) hidden.

    The other participant keeps their copy. What both have cleared is erased.
    """
    cleared, unread = _cleared_column(chat, user_id), _unread_column(chat, user_id)
    with db.transaction():
        current = get(chat["id"])
        if current is None:
            return 0, 0
        floor = cleared_upto(current, user_id)
        newest = int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM chat_messages WHERE chat_id = ?",
                               (chat["id"],), default=0) or 0)
        hidden = store.count(DM, chat["id"], floor=floor, reader=user_id)  # all of them are up to *newest*
        db.execute(f"UPDATE chats SET {cleared} = MAX({cleared}, ?), {unread} = 0 WHERE id = ?",
                   (newest, chat["id"]))
        both = int(db.scalar("SELECT MIN(cleared_upto_user1, cleared_upto_user2) FROM chats WHERE id = ?",
                             (chat["id"],), default=0) or 0)
        files = store.clear(DM, chat["id"], upto=both) if both else []
    store.remove_files(files)
    return hidden


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
