"""Temporary pages, accounts, roles and timed page visibility.

Four schedules, each one row per page or user (1.4 tables):

* ``temp_pages`` - delete a page at ``expires_at``;
* ``temp_page_index_state`` - hide or show a page now (``target_state`` into
  ``pages.is_deindexed``) and put back ``restore_state`` at ``expires_at``;
* ``temp_users`` - delete an account at ``expires_at``;
* ``temp_roles`` - put a user's role back to ``original_role`` at ``expires_at``.

The expiry job runs only while the feature is enabled (1.4 ran it even when
the plugin was off). Every item is handled in its own transaction after
re-reading it, so several workers or a re-run never act twice. Deletions go
through the pages service and :mod:`accounts`, so ``page.deleted`` and
``user.deleted`` fire as for any other deletion. The last administrator and
the last owner are never deleted or demoted; their schedule stays until an
administrator removes it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ....core.timeutil import now_sql, to_sql, utcnow
from ... import accounts
from ...db import db
from ...permissions import ADMIN_ROLES, ROLE_RANK
from ...registry import emit
from ..admin import service as admin_service
from ..pages import service as pages

REVERTIBLE_ROLES = ("user", "editor", "admin")


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


# ── Scheduling ───────────────────────────────────────────────────────────────


def schedule_page_deletion(page: dict[str, Any], expires: datetime | None, *, show_countdown: bool,
                           actor: dict[str, Any]) -> None:
    if page.get("is_home"):
        raise TemporaryError("temporary_accounts.error.home")
    _upsert("temp_pages", "page_id", page["id"], {
        "expires_at": _future(expires), "show_countdown": 1 if show_countdown else 0, "set_by": actor["id"]})


def schedule_visibility(page: dict[str, Any], *, hide: bool, expires: datetime | None, show_countdown: bool,
                        actor: dict[str, Any]) -> None:
    """Hide (or show) *page* now and put its previous visibility back at *expires*."""
    if page.get("is_home"):
        raise TemporaryError("temporary_accounts.error.home")
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


def schedule_account_deletion(target: dict[str, Any], expires: datetime | None, *, show_countdown: bool,
                              actor: dict[str, Any]) -> None:
    _check_account_target(target, actor)
    _upsert("temp_users", "user_id", target["id"], {
        "expires_at": _future(expires), "show_countdown": 1 if show_countdown else 0, "set_by": actor["id"]})


def schedule_role_revert(target: dict[str, Any], original_role: str, expires: datetime | None, *,
                         show_countdown: bool, actor: dict[str, Any]) -> None:
    _check_account_target(target, actor)
    if original_role not in REVERTIBLE_ROLES:
        raise TemporaryError("temporary_accounts.error.role_invalid")
    if ROLE_RANK[original_role] >= ROLE_RANK.get(target["role"], 0):
        raise TemporaryError("temporary_accounts.error.role_not_lower", role=original_role)
    _upsert("temp_roles", "user_id", target["id"], {
        "original_role": original_role, "expires_at": _future(expires),
        "show_countdown": 1 if show_countdown else 0, "set_by": actor["id"]})


_TABLES = {"page": ("temp_pages", "page_id"), "visibility": ("temp_page_index_state", "page_id"),
           "user": ("temp_users", "user_id"), "role": ("temp_roles", "user_id")}


def remove(kind: str, key: Any) -> bool:
    """Drop a schedule and keep things as they are now."""
    table, column = _TABLES[kind]
    return bool(db.execute(f"DELETE FROM {table} WHERE {column} = ?", (key,)).rowcount)


# ── Reading ──────────────────────────────────────────────────────────────────


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


# ── Expiry job ───────────────────────────────────────────────────────────────


def _admins() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM users WHERE role IN ('admin', 'owner')", default=0))


def _expire_page(page_id: int, now: str) -> None:
    with db.transaction():
        row = db.one("SELECT expires_at FROM temp_pages WHERE page_id = ?", (page_id,))
        page = pages.get(page_id)
        if row is None or row["expires_at"] > now:
            return
        remove("page", page_id)
        if page is not None and not page["is_home"]:
            pages.delete(page, actor_id=None)


def _expire_visibility(page_id: int, now: str) -> None:
    with db.transaction():
        row = db.one("SELECT * FROM temp_page_index_state WHERE page_id = ?", (page_id,))
        if row is None or row["expires_at"] > now:
            return
        if pages.get(page_id, with_content=False) is not None:
            pages.set_fields(page_id, is_deindexed=1 if row["restore_state"] else 0)
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
        accounts.delete(user, deleted_by=None)


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
        if current in ADMIN_ROLES and _admins() <= 1:
            return
        if current == "owner" and accounts.owners_count() <= 1:
            return
        db.execute("UPDATE users SET role = ? WHERE id = ?", (original, user_id))
        db.insert("role_history", {"user_id": user_id, "old_role": current, "new_role": original,
                                   "changed_by": None, "changed_at": now_sql()})
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
