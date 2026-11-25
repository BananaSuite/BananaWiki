"""Who may open, change and manage a Kanban board or a canvas: one set of rules for both.

Permissions (see :mod:`.permissions`) say what a person may do with a kind of
item; the item says who may see it:

* **Visibility.** ``private``: its creator and the people and roles it is
  shared with. ``members``: also every member who may view this kind of
  item. ``public``: also visitors without an account while the wiki's public
  mode is on.
* **Shares.** A share gives one person or one base role (``user`` or
  ``editor``) ``view`` or ``edit``. The higher of a person's own share and
  their role's share applies.

A signed-in person needs ``<kind>.view`` for anything. Then:

* administrators open and change everything;
* the creator opens their item, changes it with ``<kind>.edit_own``, deletes
  (or archives) it with ``<kind>.delete_own`` and manages visibility, shares
  and ownership with ``<kind>.share_own``;
* anyone else opens what its visibility or a share lets them see; they change
  it with an ``edit`` share or ``<kind>.edit_all``, and delete it with
  ``<kind>.delete_all``.

The features keep their own tables; they call these functions with what they
read from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import auth, settings
from .permissions import ADMIN_ROLES

VISIBILITIES = ("private", "members", "public")
LEVELS = ("view", "edit")
SHAREABLE_ROLES = ("user", "editor")
#: Older names still accepted from API clients: Kanban's ``write`` share level.
LEVEL_ALIASES = {"write": "edit"}
_RANK = {None: 0, "view": 1, "edit": 2}

User = dict[str, Any] | None


def level_name(value: Any) -> str | None:
    """A share level as stored (``view`` or ``edit``), or None when *value* is not one."""
    value = LEVEL_ALIASES.get(str(value or ""), str(value or ""))
    return value if value in LEVELS else None


def best(*levels: str | None) -> str | None:
    return max(levels, key=lambda level: _RANK.get(level, 0))


def is_admin(user: User) -> bool:
    return bool(user) and user.get("role") in ADMIN_ROLES  # type: ignore[union-attr]


def allowed(kind: str, verb: str, user: User) -> bool:
    return bool(user) and auth.has_permission(f"{kind}.{verb}", user)


def can_use(kind: str, user: User) -> bool:
    """Signed in and holding ``<kind>.view`` (false while the feature is off)."""
    return allowed(kind, "view", user)


def anonymous_level(visibility: str | None) -> str | None:
    return "view" if visibility == "public" and settings.public_mode_active() else None


@dataclass(frozen=True)
class Viewer:
    """What decides one person's level on every item of a kind: read once, used for a whole list."""

    user: User
    uses: bool = False
    admin: bool = False
    edit_own: bool = False
    edit_all: bool = False
    public_mode: bool = False

    @classmethod
    def of(cls, kind: str, user: User) -> Viewer:
        if user is None:
            return cls(None, public_mode=anonymous_level("public") is not None)
        uses = can_use(kind, user)
        return cls(user, uses=uses, admin=uses and is_admin(user), edit_own=uses and allowed(kind, "edit_own", user),
                   edit_all=uses and allowed(kind, "edit_all", user))

    def level(self, *, creator_id: Any, visibility: str | None, person: str | None = None, role: str | None = None,
              hidden: bool = False) -> str | None:
        """``"edit"``, ``"view"`` or None on one item.

        *person* and *role* are the levels of the user's own share and of their
        role's share; *hidden* items (archived canvases) are left to their
        creator and administrators.
        """
        if self.user is None:
            return "view" if visibility == "public" and self.public_mode and not hidden else None
        if not self.uses:
            return None
        if self.admin:
            return "edit"
        if creator_id is not None and creator_id == self.user["id"]:
            return "edit" if self.edit_own else "view"
        if hidden:
            return None
        found = best(person, role, "view" if visibility in ("members", "public") else None)
        if found and self.edit_all:
            return "edit"
        return found


def level(kind: str, user: User, *, creator_id: Any, visibility: str | None, person: str | None = None,
          role: str | None = None, hidden: bool = False) -> str | None:
    """``"edit"``, ``"view"`` or None for *user* on one item (see :meth:`Viewer.level`)."""
    return Viewer.of(kind, user).level(creator_id=creator_id, visibility=visibility, person=person, role=role,
                                       hidden=hidden)


def is_creator(user: User, creator_id: Any) -> bool:
    return bool(user) and creator_id is not None and creator_id == user["id"]  # type: ignore[index]


def can_create(kind: str, user: User) -> bool:
    return allowed(kind, "create", user)


def can_manage(kind: str, user: User, creator_id: Any) -> bool:
    """Visibility, shares and handing the item over."""
    if not can_use(kind, user):
        return False
    return is_admin(user) or (is_creator(user, creator_id) and allowed(kind, "share_own", user))


def can_delete(kind: str, user: User, creator_id: Any, visible: bool) -> bool:
    """Delete or archive (and restore) the item; *visible*: the user can open it."""
    if not can_use(kind, user) or not visible:
        return False
    if is_admin(user):
        return True
    if is_creator(user, creator_id):
        return allowed(kind, "delete_own", user)
    return allowed(kind, "delete_all", user)


def visible_condition(user: dict[str, Any], *, creator: str, visibility: str, person_share: str,
                      role_share: str) -> tuple[str, dict[str, Any]]:
    """SQL condition of the items a signed-in user who holds the view key opens (not administrators).

    *person_share* and *role_share* are ``EXISTS`` subqueries using ``:uid`` and ``:role``.
    """
    condition = (f"{creator} = :uid OR {visibility} IN ('members', 'public') OR {person_share} OR {role_share}")
    return condition, {"uid": user["id"], "role": user.get("role") or "user"}
