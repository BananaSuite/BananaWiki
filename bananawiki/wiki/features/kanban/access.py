"""Who may see, change and manage kanban boards.

Two layers decide access, as in 1.4:

* Site settings. ``kanban_access`` and ``kanban_write_access`` hold the
  weakest role with *global* view or write access (``admin``, ``editor`` or
  ``all``); ``kanban_open_access`` gives everyone global access and write
  access; ``kanban_public_access_enabled`` lets anonymous visitors read
  ``public`` boards while public mode is on. Nobody gets anything without the
  ``kanban.view`` permission (which also disappears while the feature is off).
* Boards. Administrators and the creator always have full access. A share
  row gives a role or one user ``view`` or ``write`` access. Users with global
  access also see ``public`` boards and boards shared with their role; users
  without it see only the boards they created or were individually invited to.

Write access is only ever granted on a board the user can view. Global write
access reaches every board the user can view; creating boards additionally
needs the ``kanban.create`` permission. Board settings, sharing and deletion
belong to the creator and administrators.

An archived board is read-only for everyone: nobody can write or comment on
it until its owner restores it (owners keep sharing, restore, deletion and
export).
"""

from __future__ import annotations

from typing import Any

from ... import auth, settings
from ...db import db
from ...permissions import ADMIN_ROLES, ROLE_RANK, role_at_least

VISIBILITIES = ("private", "shared", "public")
ACCESS_LEVELS = ("view", "write")
SHARE_TYPES = ("role", "user")
ACCESS_SETTING_VALUES = ("admin", "editor", "all")
SHAREABLE_ROLES = ("user", "editor", "admin")
_MINIMUM_ROLE = {"admin": "admin", "editor": "editor", "all": "user"}

User = dict[str, Any] | None
Board = dict[str, Any]


def _minimum_role(setting: str) -> str:
    return _MINIMUM_ROLE.get(str(settings.get(setting) or "admin"), "admin")


def open_access() -> bool:
    return bool(settings.get("kanban_open_access"))


def public_access_active() -> bool:
    """Anonymous visitors may read public boards (public mode + the kanban switch)."""
    return settings.public_mode_active() and bool(settings.get("kanban_public_access_enabled"))


def is_admin(user: User) -> bool:
    return bool(user) and user.get("role") in ADMIN_ROLES


def can_use(user: User) -> bool:
    """Signed in and holding ``kanban.view`` (false while the feature is off)."""
    return bool(user) and auth.has_permission("kanban.view", user)


def globally_allowed_roles() -> set[str]:
    """Base roles with global view access under the current settings."""
    if open_access():
        return set(ROLE_RANK)
    minimum = _minimum_role("kanban_access")
    return {role for role in ROLE_RANK if role_at_least(role, minimum)}


def has_global_access(user: User) -> bool:
    if not can_use(user):
        return False
    return is_admin(user) or user["role"] in globally_allowed_roles()


def can_write_global(user: User) -> bool:
    if not can_use(user):
        return False
    return is_admin(user) or open_access() or role_at_least(user["role"], _minimum_role("kanban_write_access"))


def can_create(user: User) -> bool:
    """Create or import boards: global write access and ``kanban.create``."""
    return can_write_global(user) and auth.has_permission("kanban.create", user)


def has_user_share(user: User) -> bool:
    if not user:
        return False
    return db.scalar(
        "SELECT 1 FROM kanban_board_shares WHERE share_type = 'user' AND target = ? LIMIT 1", (user["id"],)
    ) is not None


def owns_any(user: User) -> bool:
    if not user:
        return False
    return db.scalar("SELECT 1 FROM kanban_boards WHERE created_by = ? LIMIT 1", (user["id"],)) is not None


def can_open_kanban(user: User) -> bool:
    """Whether the board list (and the sidebar link) is available to *user*."""
    if user is None:
        return public_access_active()
    if not can_use(user):
        return False
    return has_global_access(user) or has_user_share(user) or owns_any(user)


def _shares_of(user: dict[str, Any], board_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT share_type, target, access_level FROM kanban_board_shares WHERE board_id = ? AND "
        "((share_type = 'user' AND target = ?) OR (share_type = 'role' AND target = ?))",
        (board_id, user["id"], user["role"]),
    )


def can_view(user: User, board: Board | None) -> bool:
    """The single read check: board page, tickets, comments, files, history, sync, embeds."""
    if board is None:
        return False
    if user is None:
        return board.get("visibility") == "public" and public_access_active()
    if not can_use(user):
        return False
    if is_admin(user) or board["created_by"] == user["id"]:
        return True
    shares = _shares_of(user, board["id"])
    if any(share["share_type"] == "user" for share in shares):
        return True
    if not has_global_access(user):
        return False
    return board.get("visibility") == "public" or any(share["share_type"] == "role" for share in shares)


def is_archived(board: Board | None) -> bool:
    return bool(board and board.get("archived_at"))


def can_write(user: User, board: Board | None) -> bool:
    """Change the board's content (never while the board is archived)."""
    return not is_archived(board) and _has_write_access(user, board)


def can_export(user: User, board: Board | None) -> bool:
    """Download the board: write access, also while archived."""
    return _has_write_access(user, board)


def _has_write_access(user: User, board: Board | None) -> bool:
    if user is None or not can_view(user, board):
        return False
    if is_admin(user) or board["created_by"] == user["id"] or can_write_global(user):  # type: ignore[index]
        return True
    return any(share["access_level"] == "write" for share in _shares_of(user, board["id"]))  # type: ignore[index]


def is_owner(user: User, board: Board | None) -> bool:
    """Manage settings and sharing, transfer and delete the board."""
    if board is None or not can_use(user):
        return False
    return is_admin(user) or board["created_by"] == user["id"]  # type: ignore[index]


def can_comment(user: User, board: Board | None) -> bool:
    return user is not None and not is_archived(board) and can_view(user, board)


def can_moderate_comment(user: User, comment: dict[str, Any]) -> bool:
    return bool(user) and (is_admin(user) or comment["user_id"] == user["id"])  # type: ignore[index]


def shareable_roles() -> list[str]:
    """Roles a board may be shared with: only roles that have global access."""
    allowed = globally_allowed_roles()
    return [role for role in SHAREABLE_ROLES if role in allowed]


# ── Board lists ───────────────────────────────────────────────────────────────

_BOARD_SELECT = (
    "SELECT b.*, COALESCE(u.username, '') AS creator_username FROM kanban_boards b "
    "LEFT JOIN users u ON u.id = b.created_by"
)


def visible_boards(user: User) -> list[dict[str, Any]]:
    """Every board *user* can open, newest first (before any saved ordering)."""
    order = " ORDER BY b.created_at DESC, b.id DESC"
    if user is None:
        if not public_access_active():
            return []
        return db.all(f"{_BOARD_SELECT} WHERE b.visibility = 'public'{order}")
    if not can_use(user):
        return []
    if is_admin(user):
        return db.all(f"{_BOARD_SELECT}{order}")
    individual = (
        "b.created_by = :uid OR EXISTS (SELECT 1 FROM kanban_board_shares s WHERE s.board_id = b.id "
        "AND s.share_type = 'user' AND s.target = :uid)"
    )
    if has_global_access(user):
        condition = (
            f"{individual} OR b.visibility = 'public' OR EXISTS (SELECT 1 FROM kanban_board_shares s "
            "WHERE s.board_id = b.id AND s.share_type = 'role' AND s.target = :role)"
        )
    else:
        condition = individual
    return db.all(f"{_BOARD_SELECT} WHERE {condition}{order}", {"uid": user["id"], "role": user["role"]})


# ── Assignees ─────────────────────────────────────────────────────────────────


def assignable_users(board: Board) -> list[dict[str, Any]]:
    """Accounts that can reach *board*, so tickets are only assigned to them."""
    shares = db.all("SELECT share_type, target FROM kanban_board_shares WHERE board_id = ?", (board["id"],))
    shared_users = {s["target"] for s in shares if s["share_type"] == "user"}
    global_roles = globally_allowed_roles()
    shared_roles = {s["target"] for s in shares if s["share_type"] == "role"} & global_roles
    public = board.get("visibility") == "public"
    users = db.all("SELECT id, username, role FROM users ORDER BY username COLLATE NOCASE")
    return [
        {"id": u["id"], "username": u["username"]}
        for u in users
        if u["role"] in ADMIN_ROLES
        or u["id"] == board["created_by"]
        or u["id"] in shared_users
        or u["role"] in shared_roles
        or (public and u["role"] in global_roles)
    ]


def assignable_ids(board: Board) -> set[str]:
    return {user["id"] for user in assignable_users(board)}
