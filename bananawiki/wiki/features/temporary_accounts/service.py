"""Temporary pages, accounts, roles and timed page visibility.

Four schedules, each one row per page or user (1.4 tables):

* ``temp_pages`` - delete a page at ``expires_at``;
* ``temp_page_index_state`` - hide or show a page now (``target_state`` into
  ``pages.is_deindexed``) and put back ``restore_state`` at ``expires_at``;
* ``temp_users`` - delete an account at ``expires_at``;
* ``temp_roles`` - put a user's role back to ``original_role`` at ``expires_at``.

The expiry job runs only while the feature is enabled; only owners and
superusers switch it off while an account schedule waits that the person
could not remove (:func:`switch_off_error`). Every item is handled in its own transaction after
re-reading it, so several workers or a re-run never act twice. Deletions go
through the pages service and :mod:`accounts`, so ``page.deleted`` and
``user.deleted`` fire as for any other deletion. A page deletion first asks
the ``page.delete`` interceptors (page protection, deletion slowdown) like a
manual one; a refused deletion keeps its schedule. Visibility follows the
``page.edit_blocked`` rules: it cannot be scheduled while the page is frozen
for the administrator, and its restore waits while the page is frozen for
whoever scheduled it. The last administrator and the last owner are never
deleted or demoted; their schedule stays until an administrator removes it.

Setting and removing a schedule is audited (``temporary.scheduled``,
``temporary.removed``), and so is the job carrying one out
(``temporary.carried_out``), with whoever set it as the actor: the
``user.deleted``, ``user.role_changed`` and ``page.deleted`` entries of the
job itself name nobody, and the schedule row goes with them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import current_app, has_request_context

from ....core.timeutil import now_sql, to_sql, utcnow
from ... import accounts
from ...db import db
from ...permissions import ADMIN_ROLES, ROLE_RANK
from ...registry import emit, intercept
from ..admin import service as admin_service
from ..audit import record
from ..pages import access as page_access
from ..pages import service as pages

REVERTIBLE_ROLES = ("user", "editor", "admin")
JOB = "temporary_accounts.expire"


class TemporaryError(ValueError):
    """A refused schedule; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


def _future(moment: datetime | None) -> str:
    if moment is None:
        raise TemporaryError("temporary_accounts.error.date_required")
    if moment <= utcnow():
        raise TemporaryError("temporary_accounts.error.date_past")
    return to_sql(moment)  # type: ignore[return-value]


def _upsert(table: str, key_column: str, key: Any, values: dict[str, Any]) -> None:
    now = now_sql()
    with db.transaction():
        if db.scalar(f"SELECT 1 FROM {table} WHERE {key_column} = ?", (key,)):
            db.update(table, {**values, "updated_at": now}, f"{key_column} = ?", (key,))
        else:
            db.insert(table, {key_column: key, **values, "created_at": now, "updated_at": now})


def _audit(action: str, kind: str, target_type: str, target: dict[str, Any], label: dict[str, Any], *,
           job_for: dict[str, Any] | None, details: dict[str, Any]) -> None:
    """Record *action*; for the job carrying out *job_for* (a schedule row), its ``set_by`` is the actor."""
    who = {} if job_for is None else {"actor_id": job_for.get("set_by"), "ip": None}
    via = {} if job_for is None else {"via": JOB}
    record(action, target_type=target_type, target_id=target["id"],
           details={"kind": kind, **label, **details, **via}, **who)


def _audit_page(action: str, kind: str, page: dict[str, Any], *, job_for: dict[str, Any] | None = None,
                **details: Any) -> None:
    _audit(action, kind, "page", page, {"title": page.get("title"), "slug": page.get("slug")}, job_for=job_for,
           details=details)


def _audit_account(action: str, kind: str, user: dict[str, Any], *, job_for: dict[str, Any] | None = None,
                   **details: Any) -> None:
    _audit(action, kind, "user", user, {"username": user.get("username")}, job_for=job_for, details=details)


def _check_account_target(target: dict[str, Any], actor: dict[str, Any]) -> None:
    if target["id"] == actor["id"]:
        raise TemporaryError("temporary_accounts.error.self")
    if target["role"] == "owner":
        raise TemporaryError("temporary_accounts.error.owner")
    if target.get("is_superuser"):
        raise TemporaryError("temporary_accounts.error.protected_account")
    # The same hierarchy as the account pages: only owners and superusers change administrators.
    error = admin_service.protection_error(actor, target)
    if error:
        raise TemporaryError(error)


# Scheduling


def schedule_page_deletion(page: dict[str, Any], expires: datetime | None, *, show_countdown: bool,
                           actor: dict[str, Any]) -> None:
    if page.get("is_home"):
        raise TemporaryError("temporary_accounts.error.home")
    blocked = page_access.edit_blocked(page, actor)
    if blocked:
        raise TemporaryError(blocked)
    expires_at = _future(expires)
    _upsert("temp_pages", "page_id", page["id"], {
        "expires_at": expires_at, "show_countdown": 1 if show_countdown else 0, "set_by": actor["id"]})
    _audit_page("temporary.scheduled", "page", page, expires_at=expires_at)


def schedule_visibility(page: dict[str, Any], *, hide: bool, expires: datetime | None, show_countdown: bool,
                        actor: dict[str, Any]) -> None:
    """Hide (or show) *page* now and put its previous visibility back at *expires*."""
    if page.get("is_home"):
        raise TemporaryError("temporary_accounts.error.home")
    # As hiding the page by hand: not while it is protected or checked out by someone else, or pending deletion.
    blocked = page_access.edit_blocked(page, actor)
    if blocked:
        raise TemporaryError(blocked)
    expires_at = _future(expires)
    target = 1 if hide else 0
    with db.transaction():
        current = pages.get(page["id"], with_content=False)
        assert current is not None
        existing = db.one("SELECT restore_state FROM temp_page_index_state WHERE page_id = ?", (page["id"],))
        restore = existing["restore_state"] if existing else current["is_deindexed"]
        if restore == target:
            raise TemporaryError("temporary_accounts.error.same_visibility")
        _upsert("temp_page_index_state", "page_id", page["id"], {
            "target_state": target, "restore_state": restore, "expires_at": expires_at,
            "show_countdown": 1 if show_countdown else 0, "set_by": actor["id"]})
        pages.set_fields(page["id"], is_deindexed=target)
        _audit_page("temporary.scheduled", "visibility", page, expires_at=expires_at, hidden=hide)


def schedule_account_deletion(target: dict[str, Any], expires: datetime | None, *, show_countdown: bool,
                              actor: dict[str, Any]) -> None:
    _check_account_target(target, actor)
    expires_at = _future(expires)
    _upsert("temp_users", "user_id", target["id"], {
        "expires_at": expires_at, "show_countdown": 1 if show_countdown else 0, "set_by": actor["id"]})
    _audit_account("temporary.scheduled", "user", target, expires_at=expires_at)


def schedule_role_revert(target: dict[str, Any], original_role: str, expires: datetime | None, *,
                         show_countdown: bool, actor: dict[str, Any]) -> None:
    _check_account_target(target, actor)
    if original_role not in REVERTIBLE_ROLES:
        raise TemporaryError("temporary_accounts.error.role_invalid")
    if ROLE_RANK[original_role] >= ROLE_RANK.get(target["role"], 0):
        raise TemporaryError("temporary_accounts.error.role_not_lower", role=original_role)
    expires_at = _future(expires)
    _upsert("temp_roles", "user_id", target["id"], {
        "original_role": original_role, "expires_at": expires_at,
        "show_countdown": 1 if show_countdown else 0, "set_by": actor["id"]})
    _audit_account("temporary.scheduled", "role", target, expires_at=expires_at, old_role=target["role"],
                   new_role=original_role)


_TABLES = {"page": ("temp_pages", "page_id"), "visibility": ("temp_page_index_state", "page_id"),
           "user": ("temp_users", "user_id"), "role": ("temp_roles", "user_id")}


def removal_error(target: dict[str, Any], actor: dict[str, Any]) -> str | None:
    """Why *actor* may not drop a schedule on *target*'s account, or None."""
    # A temporary role or a planned deletion is not undone by the account it limits.
    if target["id"] == actor["id"]:
        return None if admin_service.is_top(actor) else "temporary_accounts.error.self_remove"
    return admin_service.protection_error(actor, target)


def switch_off_error(actor: dict[str, Any]) -> str | None:
    """Why *actor* may not switch the feature off (which pauses every schedule), or None.

    Owners and superusers may. Anyone else only while they could remove every
    account schedule themselves: otherwise switching the feature off would
    stop a revert or deletion they may not cancel, such as the one ending
    their own temporary promotion.
    """
    if admin_service.is_top(actor):
        return None
    for table in ("temp_roles", "temp_users"):
        for target in db.all(f"SELECT u.* FROM {table} t JOIN users u ON u.id = t.user_id"):
            if removal_error(target, actor):
                return "temporary_accounts.error.switch_off_protected"
    return None


def remove(kind: str, key: Any, *, actor: dict[str, Any] | None = None) -> bool:
    """Drop a schedule and keep things as they are now.

    With *actor*, account schedules follow the same hierarchy as scheduling,
    and the removal is audited (the job's own removals are not).
    """
    table, column = _TABLES[kind]
    with db.transaction():
        target = None
        if actor is not None and kind in ("user", "role"):
            target = accounts.by_id(key)
            error = removal_error(target, actor) if target is not None else None
            if error:
                raise TemporaryError(error)
        row = db.one(f"SELECT * FROM {table} WHERE {column} = ?", (key,)) if actor is not None else None
        removed = bool(db.execute(f"DELETE FROM {table} WHERE {column} = ?", (key,)).rowcount)
        if removed and row is not None:
            if kind in ("user", "role"):
                _audit_account("temporary.removed", kind, target or {"id": key}, expires_at=row["expires_at"])
            else:
                page = pages.get(key, with_content=False) or {"id": key}
                _audit_page("temporary.removed", kind, page, expires_at=row["expires_at"])
        return removed


# Reading


def page_schedule(page_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM temp_pages WHERE page_id = ?", (page_id,))


def visibility_schedule(page_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM temp_page_index_state WHERE page_id = ?", (page_id,))


def account_schedule(user_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM temp_users WHERE user_id = ?", (user_id,))


def role_schedule(user_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM temp_roles WHERE user_id = ?", (user_id,))


def overview() -> dict[str, list[dict[str, Any]]]:
    return {
        "pages": db.all("SELECT t.*, p.title, p.slug FROM temp_pages t JOIN pages p ON p.id = t.page_id "
                        "ORDER BY t.expires_at"),
        "visibility": db.all("SELECT t.*, p.title, p.slug, p.is_deindexed FROM temp_page_index_state t "
                             "JOIN pages p ON p.id = t.page_id ORDER BY t.expires_at"),
        "users": db.all("SELECT t.*, u.username, u.role FROM temp_users t JOIN users u ON u.id = t.user_id "
                        "ORDER BY t.expires_at"),
        "roles": db.all("SELECT t.*, u.username, u.role AS current_role FROM temp_roles t "
                        "JOIN users u ON u.id = t.user_id ORDER BY t.expires_at"),
    }


# Expiry job


def _admins() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM users WHERE role IN ('admin', 'owner')", default=0))


def _delete_refused(page: dict[str, Any], user: dict[str, Any] | None) -> bool:
    """Whether a ``page.delete`` interceptor takes over or refuses deleting *page*."""
    if has_request_context():
        return intercept("page.delete", page=page, user=user) is not None
    # The interceptors answer with flashes and redirects; the job runs with an application context only.
    with current_app.test_request_context():
        return intercept("page.delete", page=page, user=user) is not None


def _edit_blocked(page: dict[str, Any], user: dict[str, Any] | None) -> bool:
    """Whether *page* is frozen for *user* (protection, check-out, pending deletion)."""
    if has_request_context():
        return page_access.edit_blocked(page, user) is not None
    with current_app.test_request_context():
        return page_access.edit_blocked(page, user) is not None


def _expire_page(page_id: int, now: str) -> None:
    with db.transaction():
        row = db.one("SELECT * FROM temp_pages WHERE page_id = ?", (page_id,))
        page = pages.get(page_id)
        if row is None or row["expires_at"] > now:
            return
        # Removed first so this feature's own interceptor does not refuse its own deletion.
        remove("page", page_id)
        if page is None or page["is_home"]:
            return
        setter = accounts.by_id(row["set_by"]) if row.get("set_by") else None
        if _delete_refused(page, setter):
            current = pages.get(page_id, with_content=False)
            # Deletion slowdown now owns the page; anything else (protection, …) is retried next run.
            if current is not None and not current["pending_deletion"]:
                db.insert("temp_pages", row)
            return
        _audit_page("temporary.carried_out", "page", page, job_for=row)
        pages.delete(page, actor_id=None)


def _expire_visibility(page_id: int, now: str) -> None:
    with db.transaction():
        row = db.one("SELECT * FROM temp_page_index_state WHERE page_id = ?", (page_id,))
        if row is None or row["expires_at"] > now:
            return
        page = pages.get(page_id, with_content=False)
        if page is not None:
            setter = accounts.by_id(row["set_by"]) if row.get("set_by") else None
            # Like a refused deletion, the change waits while the page is frozen for whoever scheduled it.
            if _edit_blocked(page, setter):
                return
            pages.set_fields(page_id, is_deindexed=1 if row["restore_state"] else 0)
            _audit_page("temporary.carried_out", "visibility", page, job_for=row,
                        hidden=bool(row["restore_state"]))
        remove("visibility", page_id)


def _setter_still_may(row: dict[str, Any], user: dict[str, Any]) -> bool:
    """Whoever scheduled a change to an administrator must still be allowed to make it."""
    if user["role"] not in ADMIN_ROLES and not user.get("is_superuser"):
        return True
    setter = accounts.by_id(row["set_by"]) if row.get("set_by") else None
    return setter is not None and admin_service.protection_error(setter, user) is None


def _expire_account(user_id: str, now: str) -> None:
    with db.transaction():
        row = db.one("SELECT * FROM temp_users WHERE user_id = ?", (user_id,))
        user = accounts.by_id(user_id)
        if row is None or row["expires_at"] > now or user is None:
            return
        if user["role"] == "owner" or (user["role"] in ADMIN_ROLES and _admins() <= 1):
            return
        if not _setter_still_may(row, user):
            remove("user", user_id)
            return
        _audit_account("temporary.carried_out", "user", user, job_for=row)
        accounts.delete(user, deleted_by=None)


def _changed_by_hand_since(row: dict[str, Any]) -> bool:
    """Whether someone changed the account's role after the revert was scheduled."""
    return bool(db.scalar(
        "SELECT 1 FROM role_history WHERE user_id = ? AND changed_by IS NOT NULL AND changed_at > ? LIMIT 1",
        (row["user_id"], row["updated_at"] or row["created_at"]),
    ))


def _expire_role(user_id: str, now: str) -> None:
    with db.transaction():
        row = db.one("SELECT * FROM temp_roles WHERE user_id = ?", (user_id,))
        user = accounts.by_id(user_id)
        if row is None or row["expires_at"] > now or user is None:
            return
        current, original = user["role"], row["original_role"]
        if ROLE_RANK.get(current, 0) <= ROLE_RANK.get(original, 0):
            remove("role", user_id)
            return
        # While the feature was switched off, on_role_changed did not see manual
        # changes; the role history still does. Either replaces this revert, as
        # does a scheduler who no longer outranks the account.
        if _changed_by_hand_since(row) or not _setter_still_may(row, user):
            remove("role", user_id)
            return
        if current in ADMIN_ROLES and _admins() <= 1:
            return
        if current == "owner" and accounts.owners_count() <= 1:
            return
        # As a manual role change: a custom role and overrides belong to the higher role.
        db.execute("UPDATE users SET role = ?, custom_role_id = NULL WHERE id = ?", (original, user_id))
        admin_service.clear_overrides(user_id)
        # changed_by stays NULL: _changed_by_hand_since tells the job's own changes apart by it.
        db.insert("role_history", {"user_id": user_id, "old_role": current, "new_role": original,
                                   "changed_by": None, "changed_at": now_sql()})
        _audit_account("temporary.carried_out", "role", user, job_for=row,
                       old_role=current, new_role=original)
        remove("role", user_id)
    emit("user.role_changed", user=accounts.by_id(user_id), old_role=current, new_role=original, changed_by=None)


def on_role_changed(user: dict[str, Any], old_role: str, new_role: str, changed_by: str | None) -> None:
    """A manual role change replaces any pending temporary-role revert."""
    if changed_by is not None:
        remove("role", user["id"])


def run_expiry() -> None:
    now = now_sql()
    for page_id in db.column("SELECT page_id FROM temp_page_index_state WHERE expires_at <= ?", (now,)):
        _expire_visibility(page_id, now)
    for page_id in db.column("SELECT page_id FROM temp_pages WHERE expires_at <= ?", (now,)):
        _expire_page(page_id, now)
    for user_id in db.column("SELECT user_id FROM temp_roles WHERE expires_at <= ?", (now,)):
        _expire_role(user_id, now)
    for user_id in db.column("SELECT user_id FROM temp_users WHERE expires_at <= ?", (now,)):
        _expire_account(user_id, now)
