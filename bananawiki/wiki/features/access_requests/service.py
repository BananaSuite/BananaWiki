"""Requests for edit access: what a member may ask for, and what approving gives them.

A signed-in member who can read a page but may not edit it asks, while the
wiki's ``access_requests_enabled`` setting is on (off by default). What they
ask for follows from why they may not edit (:func:`scope_for`):

* ``category``: their writing is limited to some categories and the page's
  category is not one of them. Approving adds that category to their own
  list (``user_allowed_categories``), which adds to a custom role's limits.
* ``wiki``: they may write in the page's category but their role does not
  let them edit other people's pages. Approving gives them ``page.edit_all``
  as a change of their own: it applies wherever they may write.

Either way approving also gives ``page.edit_all`` when they lack it, since
that is what editing the page needs; nothing else about the person changes,
and their custom role (if any) stays as it is. A page without a category can
only be edited by people who write everywhere, which no request can give.

One open request per person and scope; a declined one can be asked again
after :data:`COOLDOWN_DAYS`; :data:`MAX_PER_DAY` requests a day per person.
Administrators decide (no permission key manages people); the person is told
through the attention notices (and by email when the wiki sends them), and
every step is in the audit log.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ....core.timeutil import now_sql, sql_in
from ... import accounts, attention, auth, settings
from ... import permissions as perms
from ...db import db
from ..admin import service as admin_service
from ..audit.service import record
from ..pages import categories
from ..pages import service as pages

SETTING = "access_requests_enabled"
SOURCE = "access_requests.pending"
COOLDOWN_DAYS = 7
MAX_PER_DAY = 5
MAX_MESSAGE = 500
EDIT_KEY = "page.edit_all"


class RequestError(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


@dataclass(frozen=True)
class Scope:
    kind: str  # "wiki" or "category"
    category_id: int | None = None

    @property
    def category(self) -> dict[str, Any] | None:
        return categories.get(self.category_id) if self.category_id is not None else None


def enabled() -> bool:
    return bool(settings.get(SETTING, 0))


def scope_for(page: dict[str, Any], user: dict[str, Any] | None) -> Scope | None:
    """What *user* would ask for to edit *page*, or None when a request cannot help (or is not needed)."""
    if not user or auth.is_admin(user) or user.get("role") not in perms.BASE_ROLES:
        return None
    if not pages.can_view(page, user) or pages.can_edit(page, user):
        return None
    category_id = page.get("category_id")
    if auth.can_write_category(category_id, user):
        return Scope("wiki")
    if category_id is None:
        return None
    return Scope("category", int(category_id))


def _scope_of(row: dict[str, Any]) -> Scope:
    return Scope(row["scope"], row["category_id"])


def changes_for(user: dict[str, Any], scope: Scope) -> dict[str, list[Any]]:
    """What approving *scope* would change for *user* now: ``{"keys": [...], "categories": [ids]}``."""
    grants = auth.grants(user)
    if grants is None or grants.is_admin:
        return {"keys": [], "categories": []}
    found: dict[str, list[Any]] = {"keys": [], "categories": []}
    if scope.kind == "category" and not perms.can_write_category(grants, scope.category_id):
        found["categories"].append(scope.category_id)
    if EDIT_KEY not in grants.keys:
        found["keys"].append(EDIT_KEY)
    return found


# Reading


SELECT = ("SELECT r.*, u.username, c.name AS category_name, p.slug AS page_slug, p.title AS page_title, "
           "d.username AS decided_by_username FROM access_requests r JOIN users u ON u.id = r.user_id "
           "LEFT JOIN categories c ON c.id = r.category_id LEFT JOIN pages p ON p.id = r.page_id "
           "LEFT JOIN users d ON d.id = r.decided_by")


def get(request_id: int) -> dict[str, Any] | None:
    return db.one(f"{SELECT} WHERE r.id = ?", (request_id,))


def pending() -> list[dict[str, Any]]:
    return db.all(f"{SELECT} WHERE r.status = 'pending' ORDER BY r.id")


def recent_decisions(limit: int = 20) -> list[dict[str, Any]]:
    return db.all(f"{SELECT} WHERE r.status != 'pending' ORDER BY r.decided_at DESC, r.id DESC LIMIT ?", (limit,))


def count_pending() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM access_requests WHERE status = 'pending'", default=0))


def oldest_pending() -> str | None:
    return db.scalar("SELECT MIN(created_at) FROM access_requests WHERE status = 'pending'")


def _same_scope(scope: Scope) -> tuple[str, list[Any]]:
    return "scope = ? AND IFNULL(category_id, 0) = ?", [scope.kind, scope.category_id or 0]


def open_request(user_id: str, scope: Scope) -> dict[str, Any] | None:
    where, params = _same_scope(scope)
    return db.one(f"SELECT * FROM access_requests WHERE user_id = ? AND status = 'pending' AND {where}",
                  [user_id, *params])


def declined_until(user_id: str, scope: Scope) -> str | None:
    """When *user_id* may ask for *scope* again after a decline (None: they may now)."""
    where, params = _same_scope(scope)
    last = db.scalar(f"SELECT MAX(decided_at) FROM access_requests WHERE user_id = ? AND status = 'declined' "
                     f"AND decided_at > ? AND {where}", [user_id, sql_in(days=-COOLDOWN_DAYS), *params])
    if not last:
        return None
    return db.scalar("SELECT datetime(?, ?)", (last, f"+{COOLDOWN_DAYS} days"))


def offer(page: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the page shows a reader who may not edit it: the scope, and an open or declined request."""
    if not enabled():
        return None
    scope = scope_for(page, user)
    if scope is None:
        return None
    return {"scope": scope, "open": open_request(user["id"], scope),  # type: ignore[index]
            "wait_until": declined_until(user["id"], scope)}  # type: ignore[index]


# Asking


def create(user: dict[str, Any], page: dict[str, Any], message: str) -> dict[str, Any]:
    if not enabled():
        raise RequestError("access_requests.error.off")
    scope = scope_for(page, user)
    if scope is None:
        raise RequestError("access_requests.error.not_needed")
    message = (message or "").strip()
    if len(message) > MAX_MESSAGE:
        raise RequestError("access_requests.error.message_too_long", limit=MAX_MESSAGE)
    try:
        with db.transaction():
            if open_request(user["id"], scope):
                raise RequestError("access_requests.error.already_open")
            until = declined_until(user["id"], scope)
            if until:
                raise RequestError("access_requests.error.cooldown", until=until)
            filed = db.scalar("SELECT COUNT(*) FROM access_requests WHERE user_id = ? AND created_at > ?",
                              (user["id"], sql_in(hours=-24)), default=0)
            if int(filed) >= MAX_PER_DAY:
                raise RequestError("access_requests.error.too_many", limit=MAX_PER_DAY)
            request_id = db.insert("access_requests", {
                "user_id": user["id"], "scope": scope.kind, "category_id": scope.category_id,
                "page_id": page["id"], "message": message, "status": "pending", "created_at": now_sql(),
            })
    except sqlite3.IntegrityError:
        raise RequestError("access_requests.error.already_open") from None
    record("access_request.created", target_type="user", target_id=user["id"],
           details={"username": user["username"], "scope": scope.kind, "category_id": scope.category_id,
                    "page": page["slug"]})
    attention.created(SOURCE, request_id)
    row = get(request_id)
    assert row is not None
    return row


# Deciding


def _pending_or_error(request_id: int) -> dict[str, Any]:
    row = db.one("SELECT * FROM access_requests WHERE id = ?", (request_id,))
    if row is None or row["status"] != "pending":
        raise RequestError("access_requests.error.missing")
    return row


def _decide(actor: dict[str, Any], row: dict[str, Any], status: str) -> None:
    db.update("access_requests", {"status": status, "decided_by": actor["id"], "decided_at": now_sql()},
              "id = ?", (row["id"],))


def approve(actor: dict[str, Any], request_id: int) -> dict[str, Any]:
    """Give the person exactly what the request asked for (see the module docstring); returns the changes."""
    with db.transaction():
        row = _pending_or_error(request_id)
        target = accounts.by_id(row["user_id"])
        if target is None:
            raise RequestError("access_requests.error.missing")
        try:
            changes = admin_service.grant(actor, target, **changes_for(target, _scope_of(row)))
        except accounts.AccountError as error:
            raise RequestError(error.key, **error.values) from None
        _decide(actor, row, "approved")
    record("access_request.approved", target_type="user", target_id=target["id"],
           details={"username": target["username"], "request": request_id, "scope": row["scope"],
                    "category_id": row["category_id"], "permissions": " ".join(changes["keys"]) or "-",
                    "write_categories": changes["categories"] or "-"})
    attention.decided(target["id"], SOURCE, "approved", object_id=request_id)
    return changes


def decline(actor: dict[str, Any], request_id: int) -> None:
    with db.transaction():
        row = _pending_or_error(request_id)
        target = accounts.by_id(row["user_id"])
        if target is None:
            raise RequestError("access_requests.error.missing")
        _decide(actor, row, "declined")
    record("access_request.declined", target_type="user", target_id=target["id"],
           details={"username": target["username"], "request": request_id, "scope": row["scope"],
                    "category_id": row["category_id"]})
    attention.decided(target["id"], SOURCE, "denied", object_id=request_id)
