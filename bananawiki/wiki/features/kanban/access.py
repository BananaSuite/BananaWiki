"""Who may see, change and manage Kanban boards.

The rules are the ones every shared item follows (:mod:`bananawiki.wiki.sharing`):
the ``kanban.*`` permissions say what a person may do, each board's
visibility and shares say who sees it. Nobody gets anything without
``kanban.view`` (which also disappears while the feature is off).

An archived board is read-only for everyone: nobody can write or comment on
it until it is restored (whoever may delete it keeps sharing, restore,
deletion and export).
"""

from __future__ import annotations

from typing import Any

from ... import auth, sharing
from ...db import db
from ...permissions import ADMIN_ROLES

KIND = "kanban"
VISIBILITIES = sharing.VISIBILITIES
ACCESS_LEVELS = sharing.LEVELS
SHARE_TYPES = ("role", "user")
SHAREABLE_ROLES = sharing.SHAREABLE_ROLES

User = dict[str, Any] | None
Board = dict[str, Any]


def is_admin(user: User) -> bool:
    return sharing.is_admin(user)


def can_use(user: User) -> bool:
    """Signed in and holding ``kanban.view`` (false while the feature is off)."""
    return sharing.can_use(KIND, user)


def can_create(user: User) -> bool:
    """Create or import boards."""
    return sharing.can_create(KIND, user)


def can_open_kanban(user: User) -> bool:
    """Whether the board list (and the sidebar link) is available to *user*."""
    if user is None:
        return sharing.anonymous_level("public") is not None
    return can_use(user)


def _share_levels(user: dict[str, Any], board_id: int) -> tuple[str | None, str | None]:
    person = role = None
    for share in db.all(
        "SELECT share_type, access_level FROM kanban_board_shares WHERE board_id = ? AND "
        "((share_type = 'user' AND target = ?) OR (share_type = 'role' AND target = ?))",
        (board_id, user["id"], user["role"]),
    ):
        if share["share_type"] == "user":
            person = sharing.level_name(share["access_level"])
        else:
            role = sharing.level_name(share["access_level"])
    return person, role


def level(user: User, board: Board | None) -> str | None:
    """``"edit"``, ``"view"`` or None for *user* on *board* (archiving aside)."""
    if board is None:
        return None
    person = role = None
    if user is not None and not is_admin(user) and board["created_by"] != user["id"]:
        person, role = _share_levels(user, board["id"])
    return sharing.level(KIND, user, creator_id=board["created_by"], visibility=board.get("visibility"),
                         person=person, role=role)


def can_view(user: User, board: Board | None) -> bool:
    """The single read check: board page, tickets, comments, files, history, sync, embeds."""
    return level(user, board) is not None


def is_archived(board: Board | None) -> bool:
    return bool(board and board.get("archived_at"))


def can_write(user: User, board: Board | None) -> bool:
    """Change the board's content (never while the board is archived)."""
    return not is_archived(board) and _has_write_access(user, board)


def can_export(user: User, board: Board | None) -> bool:
    """Download the board: write access, also while archived."""
    return _has_write_access(user, board)


def _has_write_access(user: User, board: Board | None) -> bool:
    return user is not None and level(user, board) == "edit"


def is_owner(user: User, board: Board | None) -> bool:
    """Manage visibility and sharing, and hand the board over (``kanban.share_own``)."""
    return board is not None and sharing.can_manage(KIND, user, board["created_by"])


def can_delete(user: User, board: Board | None) -> bool:
    """Delete, archive and restore the board."""
    return board is not None and sharing.can_delete(KIND, user, board["created_by"], can_view(user, board))


def can_comment(user: User, board: Board | None) -> bool:
    return user is not None and not is_archived(board) and can_view(user, board)


def can_edit_comment(user: User, comment: dict[str, Any]) -> bool:
    """Only its author rewrites a comment: it stays credited to them."""
    return bool(user) and comment["user_id"] == user["id"]  # type: ignore[index]


def can_moderate_comment(user: User, comment: dict[str, Any]) -> bool:
    """Delete *comment*: its author, or an administrator who may change the author's account."""
    if not user:
        return False
    if comment["user_id"] == user["id"]:
        return True
    if not is_admin(user):
        return False
    from ..admin.service import protection_error  # admin imports the features

    author = db.one("SELECT id, role, is_superuser FROM users WHERE id = ?", (comment["user_id"],))
    return author is None or protection_error(user, author) is None


def shareable_roles() -> list[str]:
    """Roles a board may be shared with (administrators see every board anyway)."""
    return list(SHAREABLE_ROLES)


# Board lists

_BOARD_SELECT = (
    "SELECT b.*, COALESCE(u.username, '') AS creator_username FROM kanban_boards b "
    "LEFT JOIN users u ON u.id = b.created_by"
)


def _board_condition(user: User) -> tuple[str, dict[str, Any]] | None:
    """The SQL condition (on ``b``) of the boards *user* can open, or None when there are none."""
    if user is None:
        return ("b.visibility = 'public'", {}) if sharing.anonymous_level("public") else None
    if not can_use(user):
        return None
    if is_admin(user):
        return "1", {}
    return sharing.visible_condition(
        user, creator="b.created_by", visibility="b.visibility",
        person_share="EXISTS (SELECT 1 FROM kanban_board_shares s WHERE s.board_id = b.id "
                     "AND s.share_type = 'user' AND s.target = :uid)",
        role_share="EXISTS (SELECT 1 FROM kanban_board_shares s WHERE s.board_id = b.id "
                   "AND s.share_type = 'role' AND s.target = :role)",
    )


def visible_board_ids(user: User) -> tuple[set[int], set[int]]:
    """Ids of the active and of the archived boards *user* can open (only the ids are read)."""
    found = _board_condition(user)
    if found is None:
        return set(), set()
    condition, params = found
    active: set[int] = set()
    archived: set[int] = set()
    for row in db.all(f"SELECT b.id, COALESCE(b.archived_at, '') != '' AS archived FROM kanban_boards b "
                      f"WHERE {condition}", params):
        (archived if row["archived"] else active).add(row["id"])
    return active, archived


def visible_boards(user: User) -> list[dict[str, Any]]:
    """Every board *user* can open, newest first (before any saved ordering)."""
    found = _board_condition(user)
    if found is None:
        return []
    condition, params = found
    return db.all(f"{_BOARD_SELECT} WHERE {condition} ORDER BY b.created_at DESC, b.id DESC", params)


def ordered_boards(user: User, order_key: str | None, *, archived: bool, limit: int | None = None,
                   offset: int = 0) -> list[dict[str, Any]]:
    """The active (or *archived*) boards *user* can open: those in the saved order of *order_key*
    first, in that order, then the others newest first; with *limit*, only that window of them."""
    found = _board_condition(user)
    if found is None:
        return []
    condition, params = found
    position = ("(SELECT MIN(o.sort_order) FROM kanban_user_board_order o "
                "WHERE o.board_id = b.id AND o.user_id IS :order_key)")
    sql = (f"{_BOARD_SELECT} WHERE ({condition}) AND COALESCE(b.archived_at, '') {'!=' if archived else '='} '' "
           f"ORDER BY {position} IS NULL, {position}, b.created_at DESC, b.id DESC")
    params = {**params, "order_key": order_key}
    if limit is not None:
        sql += " LIMIT :limit OFFSET :offset"
        params.update(limit=int(limit), offset=int(offset))
    return db.all(sql, params)


def count_boards(user: User, *, archived: bool) -> int:
    """How many active (or *archived*) boards *user* can open."""
    found = _board_condition(user)
    if found is None:
        return 0
    condition, params = found
    return int(db.scalar(f"SELECT COUNT(*) FROM kanban_boards b WHERE ({condition}) "
                         f"AND COALESCE(b.archived_at, '') {'!=' if archived else '='} ''", params) or 0)


# Assignees


def assignable_users(board: Board) -> list[dict[str, Any]]:
    """Accounts that can reach *board*, so tickets are only assigned to them."""
    shares = db.all("SELECT share_type, target FROM kanban_board_shares WHERE board_id = ?", (board["id"],))
    shared_users = {s["target"] for s in shares if s["share_type"] == "user"}
    shared_roles = {s["target"] for s in shares if s["share_type"] == "role"}
    everyone = board.get("visibility") in ("members", "public")
    users = db.all("SELECT id, username, role, custom_role_id FROM users ORDER BY username COLLATE NOCASE")
    viewers = auth.holders_of("kanban.view", users)
    return [
        {"id": u["id"], "username": u["username"]}
        for u in users
        if u["role"] in ADMIN_ROLES
        or u["id"] == board["created_by"]
        or ((everyone or u["id"] in shared_users or u["role"] in shared_roles) and u["id"] in viewers)
    ]


def assignable_ids(board: Board) -> set[str]:
    return {user["id"] for user in assignable_users(board)}
