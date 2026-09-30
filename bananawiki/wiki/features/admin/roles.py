"""Custom roles: named permission bundles with optional category restrictions.

A custom role has a base role (``user`` or ``editor``) that limits which
permissions it can hold. Its holders' ``users.role`` always equals the base
role. When a role is deleted its holders move to the replacement the
administrator picked or, by default, to the most similar surviving role (as
in 1.4): the highest Jaccard overlap of permission keys, with a bonus for the
same base role.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ....core.timeutil import now_sql
from ... import permissions as perms
from ...accounts import AccountError
from ...db import db
from ...registry import emit
from .service import clear_overrides, existing_category_ids, record_role_change

BASE_ROLES = ("user", "editor")
MAX_NAME = 100
MAX_DESCRIPTION = 500
SAME_BASE_BONUS = 0.2


@dataclass
class RoleForm:
    name: str
    description: str
    base_role: str
    keys: list[str]
    read_restricted: bool
    read_ids: list[int]
    write_restricted: bool
    write_ids: list[int]

    @classmethod
    def from_form(cls, form: Any) -> RoleForm:
        read_restricted = form.get("read_restricted") == "1"
        write_restricted = form.get("write_restricted") == "1"
        return cls(
            name=(form.get("name") or "").strip(),
            description=(form.get("description") or "").strip(),
            base_role=form.get("base_role") or "user",
            keys=form.getlist("permissions"),
            read_restricted=read_restricted,
            read_ids=existing_category_ids(form.getlist("read_category_ids")) if read_restricted else [],
            write_restricted=write_restricted,
            write_ids=existing_category_ids(form.getlist("write_category_ids")) if write_restricted else [],
        )

    def clean(self) -> RoleForm:
        """Validate, then apply the coupling rules of the permission model."""
        if not self.name:
            raise AccountError("admin.roles.error.name_required")
        if len(self.name) > MAX_NAME:
            raise AccountError("admin.roles.error.name_too_long", maximum=MAX_NAME)
        if len(self.description) > MAX_DESCRIPTION:
            raise AccountError("admin.roles.error.description_too_long", maximum=MAX_DESCRIPTION)
        if self.base_role not in BASE_ROLES:
            raise AccountError("admin.roles.error.invalid_base_role")
        self.keys = sorted(perms.sanitize(self.base_role, self.keys))
        if self.base_role != "editor":
            self.write_restricted, self.write_ids = False, []
        elif self.write_restricted:
            # Write access implies read access.
            self.read_restricted = True
            self.read_ids = sorted(set(self.read_ids) | set(self.write_ids))
        if not self.read_restricted:
            self.read_ids = []
        return self


def list_roles() -> list[dict[str, Any]]:
    return db.all(
        "SELECT r.*, (SELECT COUNT(*) FROM users u WHERE u.custom_role_id = r.id) AS user_count, "
        "(SELECT COUNT(*) FROM custom_role_permissions p WHERE p.role_id = r.id) AS permission_count "
        "FROM custom_roles r ORDER BY r.name COLLATE NOCASE"
    )


def get(role_id: int) -> dict[str, Any] | None:
    role = db.one("SELECT * FROM custom_roles WHERE id = ?", (role_id,))
    if role is None:
        return None
    role["keys"] = frozenset(db.column(
        "SELECT permission_key FROM custom_role_permissions WHERE role_id = ?", (role_id,)
    ))
    cats = db.all("SELECT category_id, access_type FROM custom_role_categories WHERE role_id = ?", (role_id,))
    role["read_ids"] = frozenset(c["category_id"] for c in cats if c["access_type"] == "read")
    role["write_ids"] = frozenset(c["category_id"] for c in cats if c["access_type"] == "write")
    return role


def holders(role_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT id, username, role FROM users WHERE custom_role_id = ? ORDER BY username COLLATE NOCASE", (role_id,)
    )


def assignable_users(role_id: int) -> list[dict[str, Any]]:
    """Accounts that could be given this role (not administrators, not already holders)."""
    return db.all(
        "SELECT id, username, role FROM users WHERE role IN ('user', 'editor') "
        "AND (custom_role_id IS NULL OR custom_role_id != ?) AND is_superuser = 0 "
        "ORDER BY username COLLATE NOCASE",
        (role_id,),
    )


def _name_taken(name: str, except_id: int | None = None) -> bool:
    row = db.one("SELECT id FROM custom_roles WHERE name = ? COLLATE NOCASE", (name,))
    return row is not None and row["id"] != except_id


def _store_grants(role_id: int, data: RoleForm) -> None:
    db.execute("DELETE FROM custom_role_permissions WHERE role_id = ?", (role_id,))
    db.executemany("INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (?, ?)",
                   [(role_id, key) for key in data.keys])
    db.execute("DELETE FROM custom_role_categories WHERE role_id = ?", (role_id,))
    rows = [(role_id, cid, "read") for cid in data.read_ids] + [(role_id, cid, "write") for cid in data.write_ids]
    db.executemany("INSERT INTO custom_role_categories (role_id, category_id, access_type) VALUES (?, ?, ?)", rows)


def create(data: RoleForm, *, actor_id: str) -> int:
    data.clean()
    now = now_sql()
    with db.transaction():
        if _name_taken(data.name):
            raise AccountError("admin.roles.error.name_taken")
        role_id = db.insert("custom_roles", {
            "name": data.name, "description": data.description, "base_role": data.base_role,
            "read_restricted": int(data.read_restricted), "write_restricted": int(data.write_restricted),
            "created_by": actor_id, "created_at": now, "updated_at": now,
        })
        _store_grants(role_id, data)
    return role_id


def update(role: dict[str, Any], data: RoleForm, *, actor_id: str) -> None:
    """Replace a role's definition; holders follow a change of base role."""
    data.clean()
    moved: list[dict[str, Any]] = []
    with db.transaction():
        if _name_taken(data.name, role["id"]):
            raise AccountError("admin.roles.error.name_taken")
        db.update("custom_roles", {
            "name": data.name, "description": data.description, "base_role": data.base_role,
            "read_restricted": int(data.read_restricted), "write_restricted": int(data.write_restricted),
            "updated_at": now_sql(),
        }, "id = ?", (role["id"],))
        _store_grants(role["id"], data)
        if data.base_role != role["base_role"]:
            moved = [u for u in holders(role["id"]) if u["role"] != data.base_role]
            for user in moved:
                db.execute("UPDATE users SET role = ? WHERE id = ?", (data.base_role, user["id"]))
                record_role_change(user["id"], user["role"], data.base_role, actor_id)
    for user in moved:
        emit("user.role_changed", user=db.one("SELECT * FROM users WHERE id = ?", (user["id"],)),
             old_role=user["role"], new_role=data.base_role, changed_by=actor_id)


def most_similar(role_id: int) -> int | None:
    """The surviving role closest to *role_id*, or None when it is the only one."""
    target = get(role_id)
    if target is None:
        return None
    best_id, best_score = None, -1.0
    for other in db.all("SELECT id, base_role FROM custom_roles WHERE id != ? ORDER BY id", (role_id,)):
        keys = set(db.column("SELECT permission_key FROM custom_role_permissions WHERE role_id = ?", (other["id"],)))
        union = target["keys"] | keys
        score = len(target["keys"] & keys) / len(union) if union else 1.0
        if other["base_role"] == target["base_role"]:
            score += SAME_BASE_BONUS
        if score > best_score:
            best_id, best_score = other["id"], score
    return best_id


def delete(role: dict[str, Any], replacement: str, *, actor_id: str) -> dict[str, Any] | None:
    """Delete *role*. *replacement* is ``"auto"``, ``"none"`` or another role's id.

    Returns the role its holders were moved to (None when they keep their
    base role with its default permissions).
    """
    if replacement == "auto":
        target_id = most_similar(role["id"])
    elif replacement == "none":
        target_id = None
    elif replacement.isdigit() and int(replacement) != role["id"]:
        target_id = int(replacement)
    else:
        raise AccountError("admin.roles.error.invalid_replacement")
    target = get(target_id) if target_id else None
    if target_id and target is None:
        raise AccountError("admin.roles.error.invalid_replacement")
    moved: list[dict[str, Any]] = []
    with db.transaction():
        users = holders(role["id"])
        for user in users:
            clear_overrides(user["id"])
            if target:
                db.execute("UPDATE users SET custom_role_id = ?, role = ? WHERE id = ?",
                           (target["id"], target["base_role"], user["id"]))
                if user["role"] != target["base_role"]:
                    record_role_change(user["id"], user["role"], target["base_role"], actor_id)
                    moved.append(user)
            else:
                db.execute("UPDATE users SET custom_role_id = NULL WHERE id = ?", (user["id"],))
        db.execute("DELETE FROM custom_roles WHERE id = ?", (role["id"],))
    for user in moved:
        emit("user.role_changed", user=db.one("SELECT * FROM users WHERE id = ?", (user["id"],)),
             old_role=user["role"], new_role=target["base_role"], changed_by=actor_id)  # type: ignore[index]
    return target
