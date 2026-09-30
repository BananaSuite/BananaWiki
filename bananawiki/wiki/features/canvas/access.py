"""Who may see, change and manage canvases.

The rules follow the kanban boards so both features behave the same way:

* Site settings. ``canvas_access`` and ``canvas_write_access`` hold the
  weakest role with *global* access and with the right to create or import
  canvases (``admin``, ``editor`` or ``all``). ``canvas_open_access`` lets
  every signed-in user view and edit every canvas.
  ``canvas_public_access_enabled`` lets anonymous visitors read ``public``
  canvases while public mode is on. Nobody gets anything without the
  ``canvas.view`` permission, which also disappears while the feature is off;
  creating additionally needs ``canvas.create``.
* Canvases. Administrators and the creator always have full access. A row in
  ``canvas__permissions`` gives one user or one role ``view``, ``edit`` or
  ``none`` (an explicit refusal); a user row wins over a role row. Users
  with global access also see ``public`` canvases and canvases shared with
  their role. Users without global access only see the canvases they created
  or were individually shared on, so a single share no longer opens every
  public canvas (a 1.4 bug). Archived canvases are only visible to their
  creator and administrators.

Settings, sharing, ownership transfer and deletion belong to the creator and
administrators.
"""

from __future__ import annotations

from typing import Any

from ... import auth, settings
from ...db import db
from ...permissions import ADMIN_ROLES, ROLE_RANK, role_at_least

VISIBILITIES = ("private", "shared", "public")
PERMISSIONS = ("view", "edit", "none")
ACCESS_SETTING_VALUES = ("admin", "editor", "all")
SHAREABLE_ROLES = ("user", "editor")
_MINIMUM_ROLE = {"admin": "admin", "editor": "editor", "all": "user"}

User = dict[str, Any] | None
Layout = dict[str, Any]


def _minimum_role(setting: str) -> str:
    return _MINIMUM_ROLE.get(str(settings.get(setting) or "admin"), "admin")


def open_access() -> bool:
    return bool(settings.get("canvas_open_access"))


def public_access_active() -> bool:
    """Anonymous visitors may read public canvases (public mode + the canvas switch)."""
    return settings.public_mode_active() and bool(settings.get("canvas_public_access_enabled"))


def is_admin(user: User) -> bool:
    return bool(user) and user.get("role") in ADMIN_ROLES  # type: ignore[union-attr]


def can_use(user: User) -> bool:
    """Signed in and holding ``canvas.view`` (false while the feature is off)."""
    return bool(user) and auth.has_permission("canvas.view", user)


def globally_allowed_roles() -> set[str]:
    """Base roles with global access under the current settings."""
    if open_access():
        return set(ROLE_RANK)
    minimum = _minimum_role("canvas_access")
    return {role for role in ROLE_RANK if role_at_least(role, minimum)}


def has_global_access(user: User) -> bool:
    if not can_use(user):
        return False
    return is_admin(user) or user["role"] in globally_allowed_roles()  # type: ignore[index]


def can_create(user: User) -> bool:
    """Create or import canvases: global write access and ``canvas.create``."""
    if not can_use(user) or not auth.has_permission("canvas.create", user):
        return False
    return is_admin(user) or open_access() or role_at_least(
        user["role"], _minimum_role("canvas_write_access")  # type: ignore[index]
    )


def has_user_share(user: User) -> bool:
    if not user:
        return False
    return db.scalar(
        "SELECT 1 FROM canvas__permissions WHERE user_id = ? AND permission != 'none' LIMIT 1", (user["id"],)
    ) is not None


def owns_any(user: User) -> bool:
    if not user:
        return False
    return db.scalar("SELECT 1 FROM canvas__layouts WHERE creator_id = ? LIMIT 1", (user["id"],)) is not None


def can_open_canvas(user: User) -> bool:
    """Whether the canvas list (and the sidebar link) is available to *user*."""
    if user is None:
        return public_access_active()
    if not can_use(user):
        return False
    return has_global_access(user) or has_user_share(user) or owns_any(user)


def _rows_for(user: dict[str, Any], layout_ids: list[int] | None = None) -> dict[int, dict[str, str]]:
    """``{layout_id: {"user": permission, "role": permission}}`` for *user*."""
    sql = "SELECT layout_id, user_id, role, permission FROM canvas__permissions WHERE (user_id = ? OR role = ?)"
    params: list[Any] = [user["id"], user["role"]]
    if layout_ids is not None:
        if not layout_ids:
            return {}
        sql += f" AND layout_id IN ({','.join('?' for _ in layout_ids)})"
        params.extend(layout_ids)
    rows: dict[int, dict[str, str]] = {}
    for row in db.all(sql, params):
        kind = "user" if row["user_id"] else "role"
        rows.setdefault(row["layout_id"], {})[kind] = row["permission"]
    return rows


def _level(user: dict[str, Any], layout: Layout, rows: dict[str, str], global_access: bool) -> str | None:
    """Effective permission of a signed-in user who holds ``canvas.view``."""
    if is_admin(user) or layout["creator_id"] == user["id"]:
        return "edit"
    if layout.get("is_archived"):
        return None
    if global_access and open_access():
        return "edit"
    if "user" in rows:
        return None if rows["user"] == "none" else rows["user"]
    if not global_access:
        return None
    if "role" in rows:
        return None if rows["role"] == "none" else rows["role"]
    return "view" if layout.get("visibility") == "public" else None


def level(user: User, layout: Layout | None) -> str | None:
    """``"edit"``, ``"view"`` or ``None`` for *user* on *layout*."""
    if layout is None:
        return None
    if user is None:
        public = layout.get("visibility") == "public" and not layout.get("is_archived")
        return "view" if public and public_access_active() else None
    if not can_use(user):
        return None
    return _level(user, layout, _rows_for(user, [layout["id"]]).get(layout["id"], {}), has_global_access(user))


def can_view(user: User, layout: Layout | None) -> bool:
    """The single read check: canvas page, data, sync, history, export and embeds."""
    return level(user, layout) is not None


def can_edit(user: User, layout: Layout | None) -> bool:
    return user is not None and level(user, layout) == "edit"


def is_owner(user: User, layout: Layout | None) -> bool:
    """Manage settings and sharing, transfer and delete the canvas."""
    if layout is None or not can_use(user):
        return False
    return is_admin(user) or layout["creator_id"] == user["id"]  # type: ignore[index]


def shareable_roles() -> list[str]:
    """Roles a canvas may be shared with: only roles that have global access."""
    allowed = globally_allowed_roles()
    return [role for role in SHAREABLE_ROLES if role in allowed]


# ── Lists ─────────────────────────────────────────────────────────────────────

LAYOUT_SELECT = (
    "SELECT l.id, l.slug, l.title, l.description, l.category_id, l.creator_id, l.created_at, l.updated_at, "
    "l.is_published, l.is_archived, l.visibility, l.version, COALESCE(u.username, '') AS creator_username "
    "FROM canvas__layouts l LEFT JOIN users u ON u.id = l.creator_id"
)


def visible_layouts(user: User) -> list[dict[str, Any]]:
    """Every canvas *user* can open with its ``level``, most recently changed first."""
    order = " ORDER BY l.updated_at DESC, l.id DESC"
    if user is None:
        if not public_access_active():
            return []
        rows = db.all(f"{LAYOUT_SELECT} WHERE l.visibility = 'public' AND l.is_archived = 0{order}")
        return [dict(row, level="view") for row in rows]
    if not can_use(user):
        return []
    layouts = db.all(f"{LAYOUT_SELECT}{order}")
    permissions = _rows_for(user)
    global_access = has_global_access(user)
    visible = []
    for layout in layouts:
        found = _level(user, layout, permissions.get(layout["id"], {}), global_access)
        if found:
            visible.append(dict(layout, level=found))
    return visible
