"""Members block other members (``chat__blocks``, schema 10).

When Alice blocks Bob:

* what Bob sends Alice in a conversation they already have is withheld
  (``chat_messages.withheld``): Bob sees it as sent, Alice never gets it, not
  even after she unblocks him, and it is not unread for her;
* Bob cannot start a conversation with Alice or add her to a group: he gets
  the answer a suspended account or one without messaging gets, and her name
  is not suggested to him. A conversation they already have opens as usual,
  and if she is already in his group he is told so, as for any member;
* Alice cannot message Bob either until she unblocks him (she is told why);
* Bob's messages in groups they share are hidden from Alice (system
  messages stay), and do not count as unread for her.

So Bob is not told he was blocked: nothing he sees differs from what an
account that stopped reading its messages would show him. He can still guess
it if he sees Alice active (posting in a group they share) while a new
conversation with her, or adding her to another group, is answered as for an
inactive account. Blocking is
private: nobody is notified, it is not written to the audit log, and only
Alice sees it (in her settings, where she can unblock Bob, and in her own data
export). Administrators keep every moderation tool: the administration pages,
and a group read by an administrator who is not in it, hide nothing.
"""

from __future__ import annotations

from typing import Any

from flask import render_template
from markupsafe import Markup

from ....core.timeutil import now_sql
from ... import auth
from ...db import db
from . import policy

TABLE = "chat__blocks"
MAX_BLOCKS = 1000


class BlockRefused(ValueError):
    """A refused block; ``key`` is the translation key of the reason."""

    def __init__(self, key: str, status: int = 400, **values: Any):
        super().__init__(key)
        self.key = key
        self.status = status
        self.values = values


def blocked(blocker_id: str, blocked_id: str) -> bool:
    """Whether *blocker_id* blocked *blocked_id*."""
    return db.scalar(f"SELECT 1 FROM {TABLE} WHERE blocker_id = ? AND blocked_id = ?",
                     (blocker_id, blocked_id)) is not None


def blocked_ids(blocker_id: str) -> set[str]:
    """Everyone *blocker_id* blocked."""
    return set(db.column(f"SELECT blocked_id FROM {TABLE} WHERE blocker_id = ?", (blocker_id,)))


def listing(blocker_id: str) -> list[dict[str, Any]]:
    """The accounts *blocker_id* blocked, with their current names, by name."""
    return db.all(
        f"SELECT b.blocked_id AS user_id, u.username, b.created_at FROM {TABLE} b "
        "JOIN users u ON u.id = b.blocked_id WHERE b.blocker_id = ? ORDER BY u.username COLLATE NOCASE",
        (blocker_id,),
    )


def block(blocker: dict[str, Any], target: dict[str, Any]) -> bool:
    """Block *target*; False when it was already blocked. Raises :class:`BlockRefused`."""
    if blocker["id"] == target["id"]:
        raise BlockRefused("chat.blocks.error.self")
    with db.transaction():
        if blocked(blocker["id"], target["id"]):
            return False
        count = int(db.scalar(f"SELECT COUNT(*) FROM {TABLE} WHERE blocker_id = ?", (blocker["id"],),
                              default=0) or 0)
        if count >= MAX_BLOCKS:
            raise BlockRefused("chat.blocks.error.limit", limit=MAX_BLOCKS)
        db.execute(f"INSERT INTO {TABLE} (blocker_id, blocked_id, created_at) VALUES (?, ?, ?)",
                   (blocker["id"], target["id"], now_sql()))
        # Their unread messages stop counting for the blocker (the unread badges would keep
        # pointing at a conversation the blocker chose to silence).
        db.execute("UPDATE chats SET unread_count_user1 = 0 WHERE user1_id = ? AND user2_id = ?",
                   (blocker["id"], target["id"]))
        db.execute("UPDATE chats SET unread_count_user2 = 0 WHERE user2_id = ? AND user1_id = ?",
                   (blocker["id"], target["id"]))
    return True


def unblock(blocker_id: str, blocked_id: str) -> bool:
    """Lift a block; False when there was none."""
    return db.execute(f"DELETE FROM {TABLE} WHERE blocker_id = ? AND blocked_id = ?",
                      (blocker_id, blocked_id)).rowcount > 0


def dm_refusal(sender_id: str, recipient_id: str, *, starting: bool = False) -> str | None:
    """Why *sender_id* may not message *recipient_id* directly (a translation key), or None.

    When *recipient_id* blocked the sender, starting a conversation gets the
    answer an unknown or inactive name gets; in a conversation they already
    have, sending is not refused (the message is withheld, see :func:`.dms.send`).
    """
    if blocked(sender_id, recipient_id):
        return "chat.blocks.error.you_blocked"
    if starting and blocked(recipient_id, sender_id):
        return "chat.error.user_unavailable"
    return None


def render_settings_section(**_context: Any) -> Markup | str:
    """Slot ``account.settings_sections``: the people the user blocked, and blocking someone by name."""
    user = auth.current_user()
    if not user or not (policy.dm_allowed(user) or policy.group_allowed(user)):
        return ""
    return Markup(render_template("chat/_settings_blocks.html", blocks=listing(user["id"])))
