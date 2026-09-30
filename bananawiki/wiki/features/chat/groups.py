"""Group chats: rooms with an invite code, members with roles, and one global room.

Membership rows (``group_members``) carry the role (``owner``, ``moderator``,
``member``), a timeout (``timed_out_until``), the unread counter and the ban
flag. A ban keeps the row (``banned = 1``) so the user cannot rejoin; every
"is this user in the group" question must therefore ignore banned rows,
which :func:`member` does. Lifting a ban deletes the row.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import string
from typing import Any

from ....core.timeutil import now_sql
from ... import storage
from ...db import db
from . import store
from .store import GROUP

NAME_MAX = 100
DESCRIPTION_MAX = 500
INVITE_LENGTH = 10
CUSTOM_CODE_MIN = 6
CUSTOM_CODE_MAX = 32
CUSTOM_CODE = re.compile(rf"^[A-Za-z0-9]{{{CUSTOM_CODE_MIN},{CUSTOM_CODE_MAX}}}$")
GLOBAL_NAME = "Global Chat"
# Stored for indefinite timeouts; compares after every real timestamp.
INDEFINITE = "9999-12-31 23:59:59"
MODERATOR_ROLES = frozenset({"owner", "moderator"})
_ALPHABET = string.ascii_letters + string.digits


class CodeTaken(ValueError):
    """The requested invite code belongs to another group."""


def new_invite_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(INVITE_LENGTH))


def get(group_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM group_chats WHERE id = ?", (group_id,))


def by_invite(code: str) -> dict[str, Any] | None:
    if not code or len(code) > CUSTOM_CODE_MAX:
        return None
    return db.one("SELECT * FROM group_chats WHERE invite_code = ?", (code,))


def membership(group_id: int, user_id: str) -> dict[str, Any] | None:
    """The raw membership row, banned or not."""
    return db.one("SELECT * FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id))


def member(group_id: int, user_id: str) -> dict[str, Any] | None:
    """The membership row of a member who is not banned."""
    row = membership(group_id, user_id)
    return row if row and not row["banned"] else None


def is_timed_out(row: dict[str, Any] | None) -> bool:
    return bool(row and row["timed_out_until"] and row["timed_out_until"] > now_sql())


def is_indefinite(until: str | None) -> bool:
    return bool(until) and str(until).startswith("9999")


def system(group_id: int, key: str, **values: Any) -> int:
    return store.insert(GROUP, group_id, None, store.encode_system(key, **values), system=True)


def create(name: str, description: str, creator: dict[str, Any]) -> dict[str, Any]:
    with db.transaction():
        group_id = _insert_group(name, description, creator["id"], is_global=False)
        db.insert("group_members", {"group_id": group_id, "user_id": creator["id"], "role": "owner",
                                    "joined_at": now_sql()})
        system(group_id, "chat.system.created", user=creator["username"])
    return get(group_id)  # type: ignore[return-value]


def _insert_group(name: str, description: str, creator_id: str | None, *, is_global: bool) -> int:
    for _attempt in range(5):
        try:
            with db.transaction():
                return db.insert("group_chats", {"name": name, "description": description, "creator_id": creator_id,
                                                 "invite_code": new_invite_code(), "is_global": 1 if is_global else 0,
                                                 "created_at": now_sql()})
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Could not generate a unique invite code")


def find_global() -> dict[str, Any] | None:
    """The global room, if it exists (reads only)."""
    return db.one("SELECT * FROM group_chats WHERE is_global = 1 ORDER BY id LIMIT 1")


def global_group() -> dict[str, Any]:
    """The global room; created when missing (migration 4 creates it, joining recreates it)."""
    with db.transaction():
        row = db.one("SELECT * FROM group_chats WHERE is_global = 1 ORDER BY id LIMIT 1")
        if row is None:
            row = get(_insert_group(GLOBAL_NAME, "", None, is_global=True))
    return row  # type: ignore[return-value]


def add_member(group_id: int, user_id: str, role: str = "member") -> bool:
    cursor = db.execute(
        "INSERT OR IGNORE INTO group_members (group_id, user_id, role, joined_at) VALUES (?, ?, ?, ?)",
        (group_id, user_id, role, now_sql()),
    )
    return cursor.rowcount > 0


def remove_member(group_id: int, user_id: str) -> None:
    """Remove a member; their profile badge choice for the group goes too."""
    db.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id))
    db.execute("DELETE FROM profile_group_badges WHERE group_id = ? AND user_id = ?", (group_id, user_id))


def ban(group_id: int, user_id: str) -> None:
    db.execute("UPDATE group_members SET banned = 1, unread_count = 0 WHERE group_id = ? AND user_id = ?",
               (group_id, user_id))
    db.execute("DELETE FROM profile_group_badges WHERE group_id = ? AND user_id = ?", (group_id, user_id))


def set_role(group_id: int, user_id: str, role: str) -> None:
    db.execute("UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ?", (role, group_id, user_id))


def set_timeout(group_id: int, user_id: str, until: str | None) -> None:
    db.execute("UPDATE group_members SET timed_out_until = ? WHERE group_id = ? AND user_id = ?",
               (until, group_id, user_id))


def transfer(group_id: int, old_owner_id: str, new_owner_id: str) -> None:
    set_role(group_id, old_owner_id, "moderator")
    set_role(group_id, new_owner_id, "owner")
    db.execute("UPDATE group_chats SET creator_id = ? WHERE id = ?", (new_owner_id, group_id))


def take_over(group_id: int, admin_id: str) -> None:
    """Make a site administrator the owner; the previous owner becomes a moderator."""
    row = membership(group_id, admin_id)
    if row and row["banned"]:
        db.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, admin_id))
    add_member(group_id, admin_id)
    db.execute("UPDATE group_members SET role = 'moderator' WHERE group_id = ? AND role = 'owner' AND user_id != ?",
               (group_id, admin_id))
    set_role(group_id, admin_id, "owner")
    db.execute("UPDATE group_chats SET creator_id = ? WHERE id = ? AND is_global = 0", (admin_id, group_id))


def set_invite_code(group_id: int, code: str | None = None) -> str:
    """Replace the invite code (random unless *code* is given); the old one stops working."""
    for _attempt in range(5):
        candidate = code or new_invite_code()
        try:
            with db.transaction():
                db.execute("UPDATE group_chats SET invite_code = ? WHERE id = ?", (candidate, group_id))
            return candidate
        except sqlite3.IntegrityError as error:
            if code:
                raise CodeTaken(code) from error
    raise RuntimeError("Could not generate a unique invite code")


def set_active(group_id: int, active: bool) -> None:
    db.execute("UPDATE group_chats SET is_active = ? WHERE id = ?", (1 if active else 0, group_id))


def members(group_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT gm.user_id, gm.role, gm.timed_out_until, gm.joined_at, u.username FROM group_members gm "
        "JOIN users u ON u.id = gm.user_id WHERE gm.group_id = ? AND gm.banned = 0 "
        "ORDER BY CASE gm.role WHEN 'owner' THEN 0 WHEN 'moderator' THEN 1 ELSE 2 END, u.username COLLATE NOCASE",
        (group_id,),
    )


def banned_members(group_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT gm.user_id, gm.role, u.username FROM group_members gm JOIN users u ON u.id = gm.user_id "
        "WHERE gm.group_id = ? AND gm.banned = 1 ORDER BY u.username COLLATE NOCASE",
        (group_id,),
    )


def list_for(user_id: str) -> list[dict[str, Any]]:
    """Groups the user belongs to (never those they are banned from), most recent activity first."""
    return db.all(
        "SELECT gc.id, gc.name, gc.description, gc.is_global, gc.is_active, gc.created_at, gm.role AS my_role, "
        "gm.unread_count, m.content AS last_message, m.is_deleted AS last_deleted, m.is_system AS last_system, "
        "m.created_at AS last_message_at, "
        "(SELECT COUNT(*) FROM group_members WHERE group_id = gc.id AND banned = 0) AS member_count "
        "FROM group_members gm JOIN group_chats gc ON gc.id = gm.group_id "
        "LEFT JOIN group_messages m ON m.id = (SELECT MAX(id) FROM group_messages WHERE group_id = gc.id) "
        "WHERE gm.user_id = ? AND gm.banned = 0 "
        "ORDER BY COALESCE(m.created_at, gc.created_at) DESC, gc.id DESC",
        (user_id,),
    )


def unread_total(user_id: str) -> int:
    return int(db.scalar(
        "SELECT COALESCE(SUM(unread_count), 0) FROM group_members WHERE user_id = ? AND banned = 0",
        (user_id,), default=0,
    ) or 0)


def mark_read(group_id: int, user_id: str) -> None:
    db.execute(
        "UPDATE group_members SET unread_count = 0 WHERE group_id = ? AND user_id = ? AND unread_count > 0",
        (group_id, user_id),
    )


def send(group: dict[str, Any], sender_id: str, content: str, *, ip_address: str,
         stored: storage.StoredFile | None) -> int:
    with db.transaction():
        message_id = store.insert(GROUP, group["id"], sender_id, content, ip_address=ip_address, stored=stored)
        db.execute(
            "UPDATE group_members SET unread_count = unread_count + 1 WHERE group_id = ? AND user_id != ? "
            "AND banned = 0",
            (group["id"], sender_id),
        )
    return message_id


def clear(group_id: int, actor: dict[str, Any]) -> None:
    with db.transaction():
        files = store.clear(GROUP, group_id)
        db.execute("UPDATE group_members SET unread_count = 0 WHERE group_id = ?", (group_id,))
        system(group_id, "chat.system.cleared", actor=actor["username"])
    store.remove_files(files)


def delete(group_id: int) -> None:
    """Delete a group with its messages, members, attachments and badge choices."""
    with db.transaction():
        files = store.clear(GROUP, group_id)
        db.execute("DELETE FROM group_chats WHERE id = ?", (group_id,))
    store.remove_files(files)


def admin_page(*, page: int, per_page: int) -> tuple[list[dict[str, Any]], int]:
    total = int(db.scalar("SELECT COUNT(*) FROM group_chats", default=0) or 0)
    rows = db.all(
        "SELECT gc.id, gc.name, gc.is_global, gc.is_active, gc.created_at, u.username AS creator_name, "
        "(SELECT COUNT(*) FROM group_members WHERE group_id = gc.id AND banned = 0) AS member_count, "
        "(SELECT COUNT(*) FROM group_messages WHERE group_id = gc.id) AS message_count, "
        "m.created_at AS last_message_at "
        "FROM group_chats gc LEFT JOIN users u ON u.id = gc.creator_id "
        "LEFT JOIN group_messages m ON m.id = (SELECT MAX(id) FROM group_messages WHERE group_id = gc.id) "
        "ORDER BY gc.is_global DESC, COALESCE(m.created_at, gc.created_at) DESC, gc.id DESC LIMIT ? OFFSET ?",
        (per_page, (page - 1) * per_page),
    )
    return rows, total
