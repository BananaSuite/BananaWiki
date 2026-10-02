"""Account administration: who may change whom, and the changes themselves.

Hierarchy
---------
* Nobody changes their own role, suspends or deletes themselves here, except
  that an owner may step down while another owner remains.
* Superusers (``users.is_superuser``) are protected: only they change their
  own account. Only superusers grant or revoke superuser status.
* Owners are changed only by themselves. Only owners and superusers make
  someone an owner.
* Administrators are changed only by owners and superusers, so one
  administrator cannot take over another by resetting their password,
  demoting or suspending them (1.4 audit M4).
* The last owner can never be demoted or deleted.

Every function takes the acting user and raises :class:`AccountError` with a
translation key when the change is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ....core.timeutil import now_sql, to_sql, utcnow
from ... import accounts, attention, auth
from ... import permissions as perms
from ...accounts import AccountError
from ...db import db
from ...registry import emit

MAX_SUSPEND_REASON = 500
USERS_PER_PAGE = 50
SUSPENSION_PRESETS = {"1": 1, "8": 8, "24": 24, "72": 72, "168": 168, "720": 720}


# ── Rules ────────────────────────────────────────────────────────────────────


def is_superuser(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("is_superuser"))


def _is_top(user: dict[str, Any]) -> bool:
    return user.get("role") == "owner" or is_superuser(user)


def protection_error(actor: dict[str, Any], target: dict[str, Any]) -> str | None:
    """Why *actor* may not change *target*'s account, or None."""
    if actor["id"] == target["id"]:
        return None
    if is_superuser(target):
        return "admin.users.error.protected"
    if target["role"] == "owner":
        return "admin.users.error.owner_only_self"
    if target["role"] == "admin" and not _is_top(actor):
        return "admin.users.error.admin_needs_owner"
    return None


def require_manageable(actor: dict[str, Any], target: dict[str, Any]) -> None:
    error = protection_error(actor, target)
    if error:
        raise AccountError(error)


def _current_accounts(actor: dict[str, Any], target: dict[str, Any], *, manageable: bool = True
                      ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reload account authority inside the caller's write transaction.

    A request's snapshots may precede a promotion, suspension or password
    reset. Check those changes before changing another account.
    """
    current_actor = accounts.by_id(actor["id"])
    current_target = accounts.by_id(target["id"])
    if current_actor is None or current_target is None or not auth.is_admin(current_actor) \
            or auth.account_block(current_actor) or current_actor["password"] != actor["password"]:
        raise AccountError("error.forbidden")
    if manageable:
        require_manageable(current_actor, current_target)
    return current_actor, current_target


def _require_other(actor: dict[str, Any], target: dict[str, Any], key: str) -> None:
    if actor["id"] == target["id"]:
        raise AccountError(key)


def impersonation_error(actor: dict[str, Any], target: dict[str, Any]) -> str | None:
    if actor["id"] == target["id"]:
        return "admin.impersonate.error.self"
    if (target["role"] in perms.ADMIN_ROLES or is_superuser(target)) and not is_superuser(actor):
        return "admin.impersonate.error.superuser_only"
    if auth.account_block(target):
        return "admin.impersonate.error.blocked"
    return None


# ── Queries ──────────────────────────────────────────────────────────────────


def get_user(user_id: str) -> dict[str, Any] | None:
    return accounts.by_id(user_id)


def pending_count() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM users WHERE approval_status = 'pending'", default=0))


def pending_oldest() -> str | None:
    return db.scalar("SELECT MIN(created_at) FROM users WHERE approval_status = 'pending'")


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@dataclass
class UserFilter:
    query: str = ""
    role: str = ""
    status: str = ""
    approval: str = ""
    custom_role: int | None = None

    @classmethod
    def from_args(cls, args: Any) -> UserFilter:
        role = args.get("role", "")
        status = args.get("status", "")
        approval = args.get("approval", "")
        custom = args.get("custom_role", "")
        return cls(
            query=(args.get("q") or "").strip()[:50],
            role=role if role in perms.ROLES else "",
            status=status if status in ("active", "suspended") else "",
            approval=approval if approval in ("pending", "approved", "denied") else "",
            custom_role=int(custom) if custom.isdigit() else None,
        )

    def as_args(self) -> dict[str, Any]:
        values = {"q": self.query, "role": self.role, "status": self.status, "approval": self.approval,
                  "custom_role": self.custom_role or ""}
        return {key: value for key, value in values.items() if value}

    def where(self) -> tuple[str, list[Any]]:
        clauses, params = ["1 = 1"], []
        if self.query:
            clauses.append("u.username LIKE ? ESCAPE '\\'")
            params.append(_like(self.query))
        if self.role:
            clauses.append("u.role = ?")
            params.append(self.role)
        if self.status == "suspended":
            clauses.append("u.suspended = 1")
        elif self.status == "active":
            clauses.append("u.suspended = 0")
        if self.approval:
            clauses.append("u.approval_status = ?")
            params.append(self.approval)
        if self.custom_role:
            clauses.append("u.custom_role_id = ?")
            params.append(self.custom_role)
        return " AND ".join(clauses), params


def list_users(flt: UserFilter, page: int = 1, per_page: int = USERS_PER_PAGE) -> tuple[list[dict[str, Any]], int]:
    where, params = flt.where()
    total = int(db.scalar(f"SELECT COUNT(*) FROM users u WHERE {where}", params, default=0))
    rows = db.all(
        "SELECT u.id, u.username, u.role, u.is_superuser, u.suspended, u.suspended_until, u.approval_status, "
        "u.created_at, u.last_login_at, u.chat_disabled, u.custom_role_id, cr.name AS custom_role_name "
        f"FROM users u LEFT JOIN custom_roles cr ON cr.id = u.custom_role_id WHERE {where} "
        "ORDER BY u.username COLLATE NOCASE LIMIT ? OFFSET ?",
        [*params, per_page, (max(page, 1) - 1) * per_page],
    )
    return rows, total


def active_sessions(user_id: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
    """Active sign-in sessions, newest activity first (one user's, or everyone's)."""
    where, params = "s.revoked_at IS NULL AND s.expires_at > ?", [now_sql()]
    if user_id:
        where += " AND s.user_id = ?"
        params.append(user_id)
    return db.all(
        "SELECT s.id, s.user_id, s.created_at, s.last_seen_at, s.expires_at, s.remember_me, s.auth_method, "
        "s.ip_address, s.last_ip, s.user_agent, u.username FROM user_sessions s JOIN users u ON u.id = s.user_id "
        f"WHERE {where} ORDER BY s.last_seen_at DESC LIMIT ?",
        [*params, limit],
    )


def audit_trail(user_id: str) -> dict[str, list[dict[str, Any]]]:
    """Everything the database records about changes to one account."""
    return {
        "usernames": db.all(
            "SELECT * FROM username_history WHERE user_id = ? ORDER BY changed_at DESC, id DESC", (user_id,)
        ),
        "roles": db.all(
            "SELECT rh.*, u.username AS changed_by_username FROM role_history rh "
            "LEFT JOIN users u ON u.id = rh.changed_by WHERE rh.user_id = ? ORDER BY rh.changed_at DESC, rh.id DESC",
            (user_id,),
        ),
        "suspensions": db.all(
            "SELECT sa.*, u.username AS performed_by_username FROM suspension_audit sa "
            "LEFT JOIN users u ON u.id = sa.performed_by WHERE sa.user_id = ? "
            "ORDER BY sa.created_at DESC, sa.id DESC",
            (user_id,),
        ),
        "impersonated": db.all(
            "SELECT l.*, u.username AS other_username FROM impersonation_logs l "
            "LEFT JOIN users u ON u.id = l.admin_id WHERE l.target_user_id = ? ORDER BY l.started_at DESC LIMIT 100",
            (user_id,),
        ),
        "impersonating": db.all(
            "SELECT l.*, u.username AS other_username FROM impersonation_logs l "
            "LEFT JOIN users u ON u.id = l.target_user_id WHERE l.admin_id = ? ORDER BY l.started_at DESC LIMIT 100",
            (user_id,),
        ),
        "invites": db.all(
            "SELECT ic.code, icu.used_at FROM invite_code_usage icu JOIN invite_codes ic ON ic.id = icu.invite_code_id "
            "WHERE icu.user_id = ? ORDER BY icu.used_at DESC",
            (user_id,),
        ),
    }


def contribution_count(user_id: str) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM page_history WHERE edited_by = ?", (user_id,), default=0))


# ── Account changes ──────────────────────────────────────────────────────────


def create_user(username: str, password: str, role: str, *, force_password_change: bool) -> dict[str, Any]:
    """Create an account from the admin form (owners are only ever promoted, never created)."""
    if role not in ("user", "editor", "admin"):
        raise AccountError("auth.error.invalid_role")
    return accounts.create(username, password, role=role, force_password_change=force_password_change)


def rename(actor: dict[str, Any], target: dict[str, Any], new_username: str) -> dict[str, Any]:
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        return accounts.rename(target, new_username, changed_by=actor["id"])


def clear_overrides(user_id: str) -> None:
    """Drop per-user permission overrides so the role defaults apply again."""
    db.execute("DELETE FROM user_permissions WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM user_category_access WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM user_allowed_categories WHERE user_id = ?", (user_id,))


def record_role_change(user_id: str, old_role: str, new_role: str, changed_by: str | None) -> None:
    db.execute(
        "INSERT INTO role_history (user_id, old_role, new_role, changed_by, changed_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, old_role, new_role, changed_by, now_sql()),
    )


def change_role(actor: dict[str, Any], target: dict[str, Any], new_role: str) -> dict[str, Any]:
    """Set a base role (dropping any custom role and per-user overrides)."""
    if new_role not in perms.ROLES:
        raise AccountError("auth.error.invalid_role")
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        if actor["id"] == target["id"] and not (target["role"] == "owner" and new_role != "owner"):
            raise AccountError("admin.users.error.own_role")
        if new_role == "owner" and not _is_top(actor):
            raise AccountError("admin.users.error.owner_grant")
        if target["role"] == "owner" and new_role != "owner" and accounts.owners_count() <= 1:
            raise AccountError("admin.users.error.last_owner")
        if new_role == target["role"] and not target.get("custom_role_id"):
            return target
        db.execute("UPDATE users SET role = ?, custom_role_id = NULL WHERE id = ?", (new_role, target["id"]))
        clear_overrides(target["id"])
        if new_role != target["role"]:
            record_role_change(target["id"], target["role"], new_role, actor["id"])
    updated = get_user(target["id"])
    if new_role != target["role"]:
        emit("user.role_changed", user=updated, old_role=target["role"], new_role=new_role, changed_by=actor["id"])
    return updated  # type: ignore[return-value]


def assign_custom_role(actor: dict[str, Any], target: dict[str, Any], role: dict[str, Any]) -> dict[str, Any]:
    """Give *target* a custom role; its base role becomes the role's base role."""
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        if target["role"] in perms.ADMIN_ROLES:
            raise AccountError("admin.roles.error.admin_target")
        db.execute("UPDATE users SET custom_role_id = ?, role = ? WHERE id = ?",
                   (role["id"], role["base_role"], target["id"]))
        clear_overrides(target["id"])
        if role["base_role"] != target["role"]:
            record_role_change(target["id"], target["role"], role["base_role"], actor["id"])
    updated = get_user(target["id"])
    if role["base_role"] != target["role"]:
        emit("user.role_changed", user=updated, old_role=target["role"], new_role=role["base_role"],
             changed_by=actor["id"])
    return updated  # type: ignore[return-value]


def remove_custom_role(actor: dict[str, Any], target: dict[str, Any]) -> None:
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        if not target.get("custom_role_id"):
            raise AccountError("admin.roles.error.not_assigned")
        db.execute("UPDATE users SET custom_role_id = NULL WHERE id = ?", (target["id"],))
        clear_overrides(target["id"])


def suspension_end(duration: str, custom_datetime: datetime | None, rel_hours: str,
                   rel_minutes: str) -> tuple[str | None, str]:
    """Resolve the suspension form into (UTC end or None for permanent, label for the audit)."""
    if duration in ("", "permanent"):
        return None, "permanent"
    if duration in SUSPENSION_PRESETS:
        hours = SUSPENSION_PRESETS[duration]
        return to_sql(utcnow() + timedelta(hours=hours)), f"{hours}h"
    if duration == "custom_datetime":
        if custom_datetime is None:
            raise AccountError("admin.suspend.error.datetime_required")
        if custom_datetime <= utcnow():
            raise AccountError("admin.suspend.error.past")
        return to_sql(custom_datetime), "until " + to_sql(custom_datetime)  # type: ignore[operator]
    if duration == "custom_relative":
        try:
            hours, minutes = int(rel_hours or 0), int(rel_minutes or 0)
        except ValueError:
            raise AccountError("admin.suspend.error.invalid_duration") from None
        total = hours * 60 + minutes
        if hours < 0 or minutes < 0 or total <= 0:
            raise AccountError("admin.suspend.error.invalid_duration")
        if total > 60 * 24 * 3650:
            raise AccountError("admin.suspend.error.invalid_duration")
        return to_sql(utcnow() + timedelta(minutes=total)), f"{hours}h {minutes}m"
    raise AccountError("admin.suspend.error.invalid_duration")


def suspend(actor: dict[str, Any], target: dict[str, Any], *, until: str | None, label: str, reason: str,
            reason_visible: bool, time_visible: bool) -> None:
    """Suspend an account and end all of its sessions at once (1.4 audit H2)."""
    reason = (reason or "").strip()[:MAX_SUSPEND_REASON]
    reason_visible = reason_visible and bool(reason)
    time_visible = time_visible and until is not None
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        _require_other(actor, target, "admin.users.error.self_suspend")
        db.update("users", {
            "suspended": 1, "suspended_until": until, "suspend_reason": reason or None,
            "suspend_reason_visible": int(reason_visible), "suspend_time_visible": int(time_visible),
        }, "id = ?", (target["id"],))
        db.insert("suspension_audit", {
            "user_id": target["id"], "action": "suspend", "reason": reason or None,
            "reason_visible": int(reason_visible), "time_visible": int(time_visible), "duration": label,
            "suspended_until": until, "performed_by": actor["id"], "created_at": now_sql(),
        })
        auth.revoke_sessions(target["id"])
    emit("user.suspended", user=get_user(target["id"]), until=until, actor_id=actor["id"])


def unsuspend(actor: dict[str, Any], target: dict[str, Any]) -> None:
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        db.update("users", {
            "suspended": 0, "suspended_until": None, "suspend_reason": None,
            "suspend_reason_visible": 0, "suspend_time_visible": 0,
        }, "id = ?", (target["id"],))
        db.insert("suspension_audit", {
            "user_id": target["id"], "action": "unsuspend", "performed_by": actor["id"], "created_at": now_sql(),
        })


def expire_suspensions() -> int:
    """Lift timed suspensions that have run out."""
    return db.execute(
        "UPDATE users SET suspended = 0, suspended_until = NULL, suspend_reason = NULL, "
        "suspend_reason_visible = 0, suspend_time_visible = 0 "
        "WHERE suspended = 1 AND suspended_until IS NOT NULL AND suspended_until <= ?",
        (now_sql(),),
    ).rowcount


def approve(actor: dict[str, Any], target: dict[str, Any]) -> None:
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        changed = db.execute(
            "UPDATE users SET approval_status = 'approved', approved_by = ?, approved_denied_at = ? "
            "WHERE id = ? AND approval_status = 'pending'",
            (actor["id"], now_sql(), target["id"]),
        ).rowcount
        if not changed:
            raise AccountError("admin.approval.error.not_pending")
    attention.decided(target["id"], "admin.signups", "approved")


def deny(actor: dict[str, Any], target: dict[str, Any]) -> None:
    now = now_sql()
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        changed = db.execute(
            "UPDATE users SET approval_status = 'denied', denied_by = ?, denied_at = ?, approved_denied_at = ?, "
            "denied_notified = 0 WHERE id = ? AND approval_status = 'pending'",
            (actor["id"], now, now, target["id"]),
        ).rowcount
        if not changed:
            raise AccountError("admin.approval.error.not_pending")
        auth.revoke_sessions(target["id"])
    attention.decided(target["id"], "admin.signups", "denied")


def reset_password(actor: dict[str, Any], target: dict[str, Any], password: str, *, require_change: bool,
                   keep_original: bool) -> None:
    """Set a new password, signing the account out everywhere (except the actor's own session)."""
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        own = actor["id"] == target["id"]
        if keep_original and not target.get("original_password_backup"):
            db.execute("UPDATE users SET original_password_backup = ? WHERE id = ?",
                       (target["password"], target["id"]))
        accounts.set_password(target["id"], password, keep_session_id=auth.current_session_id() if own else None,
                              require_change=require_change and not own)
    emit("user.password_reset", user=get_user(target["id"]), actor_id=actor["id"])


def restore_password(actor: dict[str, Any], target: dict[str, Any]) -> None:
    """Put back the password saved before a temporary password was set."""
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        if not target.get("original_password_backup"):
            raise AccountError("admin.password.error.no_backup")
        own = actor["id"] == target["id"]
        db.execute(
            "UPDATE users SET password = original_password_backup, original_password_backup = NULL, "
            "force_password_change = 0 WHERE id = ?",
            (target["id"],),
        )
        auth.revoke_sessions(target["id"], except_session_id=auth.current_session_id() if own else None)
    emit("user.password_reset", user=get_user(target["id"]), actor_id=actor["id"])


def delete_user(actor: dict[str, Any], target: dict[str, Any]) -> None:
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        _require_other(actor, target, "admin.users.error.self_delete")
        accounts.delete(target, deleted_by=actor["id"])


def toggle_chat(actor: dict[str, Any], target: dict[str, Any]) -> bool:
    """Flip chat access; returns True when chat is now disabled."""
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        disabled = not target.get("chat_disabled")
        db.execute("UPDATE users SET chat_disabled = ? WHERE id = ?", (int(disabled), target["id"]))
    return disabled


def toggle_superuser(actor: dict[str, Any], target: dict[str, Any]) -> bool:
    """Grant or revoke superuser status; returns the new state."""
    with db.transaction():
        actor, target = _current_accounts(actor, target, manageable=False)
        if not is_superuser(actor):
            raise AccountError("admin.users.error.superuser_only")
        _require_other(actor, target, "admin.users.error.own_superuser")
        enabled = not is_superuser(target)
        db.execute("UPDATE users SET is_superuser = ? WHERE id = ?", (int(enabled), target["id"]))
    return enabled


def revoke_session(actor: dict[str, Any], target: dict[str, Any], session_id: str | None = None) -> int:
    """End one session of *target*, or all of them."""
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        if session_id is None:
            own = actor["id"] == target["id"]
            return auth.revoke_sessions(target["id"], except_session_id=auth.current_session_id() if own else None)
        return db.execute(
            "UPDATE user_sessions SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (now_sql(), session_id, target["id"]),
        ).rowcount


def mass_logout(actor: dict[str, Any]) -> int:
    """Sign everyone out except the administrator's current session."""
    session_id = auth.current_session_id()
    params: list[Any] = [now_sql()]
    extra = ""
    if session_id:
        extra = " AND id != ?"
        params.append(session_id)
    with db.transaction():
        _current_accounts(actor, actor, manageable=False)
        return db.execute(f"UPDATE user_sessions SET revoked_at = ? WHERE revoked_at IS NULL{extra}", params).rowcount


def start_impersonation(actor: dict[str, Any], target: dict[str, Any]) -> None:
    if auth.is_impersonating():
        raise AccountError("admin.impersonate.error.nested")
    with db.transaction():
        actor, target = _current_accounts(actor, target, manageable=False)
        error = impersonation_error(actor, target)
        if error:
            raise AccountError(error)
        db.execute(
            "INSERT INTO impersonation_logs (admin_id, target_user_id, started_at) VALUES (?, ?, ?)",
            (actor["id"], target["id"], now_sql()),
        )
        auth.start_impersonation(target)


# ── Attribution and history clean-up ─────────────────────────────────────────


def deattribute_all(actor: dict[str, Any], target: dict[str, Any]) -> int:
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        count = db.execute("UPDATE page_history SET edited_by = NULL WHERE edited_by = ?", (target["id"],)).rowcount
        db.execute("UPDATE pages SET last_edited_by = NULL WHERE last_edited_by = ?", (target["id"],))
    return count


def reattribute_all(actor: dict[str, Any], target: dict[str, Any], recipient: dict[str, Any]) -> int:
    if recipient["id"] == target["id"]:
        raise AccountError("admin.attributions.error.same_user")
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        actor, recipient = _current_accounts(actor, recipient)
        count = db.execute("UPDATE page_history SET edited_by = ? WHERE edited_by = ?",
                           (recipient["id"], target["id"])).rowcount
        db.execute("UPDATE pages SET last_edited_by = ? WHERE last_edited_by = ?", (recipient["id"], target["id"]))
    return count


def delete_role_history(actor: dict[str, Any], target: dict[str, Any], entry_id: int | None = None) -> int:
    # Not one's own: the history is how others see who promoted themselves.
    _require_other(actor, target, "admin.attributions.error.own_history")
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        if entry_id is None:
            return db.execute("DELETE FROM role_history WHERE user_id = ?", (target["id"],)).rowcount
        count = db.execute("DELETE FROM role_history WHERE id = ? AND user_id = ?", (entry_id, target["id"])).rowcount
        if not count:
            raise AccountError("admin.attributions.error.entry_not_found")
        return count


# ── Per-user permission overrides ────────────────────────────────────────────


@dataclass
class Access:
    """Per-user permission overrides as shown in the admin forms."""

    customized: bool
    keys: frozenset[str]
    read_restricted: bool
    read_ids: frozenset[int]
    write_restricted: bool
    write_ids: frozenset[int]


def user_access(user: dict[str, Any]) -> Access:
    rows = db.all("SELECT access_type, restricted FROM user_category_access WHERE user_id = ?", (user["id"],))
    if not rows:
        return Access(False, perms.defaults(user["role"]), False, frozenset(), False, frozenset())
    restricted = {row["access_type"]: bool(row["restricted"]) for row in rows}
    cats = db.all("SELECT category_id, access_type FROM user_allowed_categories WHERE user_id = ?", (user["id"],))
    keys = db.column("SELECT permission_key FROM user_permissions WHERE user_id = ?", (user["id"],))
    return Access(
        True,
        frozenset(keys),
        restricted.get("read", False),
        frozenset(c["category_id"] for c in cats if c["access_type"] == "read"),
        restricted.get("write", False),
        frozenset(c["category_id"] for c in cats if c["access_type"] == "write"),
    )


def existing_category_ids(values: list[str]) -> list[int]:
    wanted = {int(v) for v in values if str(v).isdigit()}
    if not wanted:
        return []
    known = set(db.column("SELECT id FROM categories"))
    return sorted(wanted & known)


def _require_overridable(actor: dict[str, Any], target: dict[str, Any]) -> None:
    require_manageable(actor, target)
    if target["role"] not in ("user", "editor"):
        raise AccountError("admin.permissions.error.admin_target")
    if target.get("custom_role_id"):
        raise AccountError("admin.permissions.error.custom_role")


def _write_access(user_id: str, access_type: str, restricted: bool, category_ids: list[int]) -> None:
    db.execute(
        "INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, access_type) DO UPDATE SET restricted = excluded.restricted",
        (user_id, access_type, int(restricted)),
    )
    db.execute("DELETE FROM user_allowed_categories WHERE user_id = ? AND access_type = ?", (user_id, access_type))
    if restricted:
        db.executemany(
            "INSERT OR IGNORE INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, ?)",
            [(user_id, category_id, access_type) for category_id in category_ids],
        )


def save_overrides(actor: dict[str, Any], target: dict[str, Any], *, keys: list[str], read_restricted: bool,
                   read_ids: list[int], write_restricted: bool, write_ids: list[int]) -> None:
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        _require_overridable(actor, target)
        granted = perms.sanitize(target["role"], keys)
        if target["role"] != "editor":
            write_restricted, write_ids = False, []
        db.execute("DELETE FROM user_permissions WHERE user_id = ?", (target["id"],))
        db.executemany("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)",
                       [(target["id"], key) for key in sorted(granted)])
        _write_access(target["id"], "read", read_restricted, read_ids)
        _write_access(target["id"], "write", write_restricted, write_ids)


def save_editor_access(actor: dict[str, Any], target: dict[str, Any], *, restricted: bool,
                       category_ids: list[int]) -> None:
    """Limit which categories an editor may write to, keeping their other permissions."""
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        _require_overridable(actor, target)
        if target["role"] != "editor":
            raise AccountError("admin.editor_access.error.not_editor")
        current = user_access(target)
        if not current.customized:
            db.executemany("INSERT OR IGNORE INTO user_permissions (user_id, permission_key) VALUES (?, ?)",
                           [(target["id"], key) for key in sorted(current.keys)])
            _write_access(target["id"], "read", False, [])
        _write_access(target["id"], "write", restricted, category_ids)


def reset_overrides(actor: dict[str, Any], target: dict[str, Any]) -> None:
    with db.transaction():
        actor, target = _current_accounts(actor, target)
        _require_overridable(actor, target)
        clear_overrides(target["id"])


# ── Automatic clean-up ───────────────────────────────────────────────────────


def delete_expired_signups(denied_hours: int, pending_hours: int) -> int:
    """Delete denied accounts after *denied_hours* and unreviewed ones after *pending_hours* (0: never)."""
    candidates = db.all(
        "SELECT * FROM users WHERE approval_status = 'denied' AND denied_at IS NOT NULL AND denied_at <= ?",
        (to_sql(utcnow() - timedelta(hours=max(denied_hours, 0))),),
    )
    if pending_hours > 0:
        candidates += db.all(
            "SELECT * FROM users WHERE approval_status = 'pending' AND created_at <= ?",
            (to_sql(utcnow() - timedelta(hours=pending_hours)),),
        )
    for user in candidates:
        accounts.delete(user)
    return len(candidates)
