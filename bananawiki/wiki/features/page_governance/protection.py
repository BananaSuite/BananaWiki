"""Page protection: one editor controls a page; nobody else may change it.

Protection is on when the feature is enabled and ``page_protection_enabled``
is set. The controller is the editor who protected the page; everyone else,
administrators included, is blocked from editing or deleting it. An
administrator who needs the page back files an unlock request; once
:data:`UNLOCK_DELAY` has passed the administrator may remove the protection.
The controller sees the pending request on the page and can release the page
earlier.

Protection lapses on its own when the controller's account is deleted
(``ON DELETE SET NULL``) or no longer holds an editing role, and it never
applies to the home page.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ....core.timeutil import now_sql, parse, to_sql, utcnow
from ... import auth, settings
from ...db import db
from ...permissions import EDITOR_ROLES
from ...registry import is_enabled
from ..pages import service as pages
from .errors import GovernanceError

UNLOCK_DELAY = timedelta(hours=72)

_CLEARED = {"protected_by": None, "protected_at": None,
            "protection_unlock_requested_at": None, "protection_unlock_requested_by": None}


def active() -> bool:
    return is_enabled("page_governance") and bool(settings.get("page_protection_enabled"))


def _row(page_id: int) -> dict[str, Any] | None:
    return db.one(
        "SELECT p.id, p.is_home, p.protected_by, p.protected_at, p.protection_unlock_requested_at, "
        "p.protection_unlock_requested_by, c.username AS controller_name, c.role AS controller_role, "
        "r.username AS requested_by_name "
        "FROM pages p LEFT JOIN users c ON c.id = p.protected_by "
        "LEFT JOIN users r ON r.id = p.protection_unlock_requested_by WHERE p.id = ?",
        (page_id,),
    )


def _describe(row: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None or row["is_home"] or not row["protected_by"] or row["controller_role"] not in EDITOR_ROLES:
        return None
    requested = parse(row["protection_unlock_requested_at"])
    ready = requested + UNLOCK_DELAY if requested else None
    return {
        "page_id": row["id"],
        "controller_id": row["protected_by"],
        "controller_name": row["controller_name"],
        "protected_at": row["protected_at"],
        "is_controller": bool(user) and user["id"] == row["protected_by"],
        "unlock_requested_at": row["protection_unlock_requested_at"],
        "unlock_requested_by_name": row["requested_by_name"],
        "unlock_ready_at": to_sql(ready),
        "unlock_ready": bool(ready and ready <= utcnow()),
    }


def state(page: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Protection of *page* as seen by *user*, or None when the page is not protected."""
    if not active():
        return None
    return _describe(_row(page["id"]), user)


def blocks(page: dict[str, Any], user: dict[str, Any] | None) -> bool:
    current = state(page, user)
    return bool(current and not current["is_controller"])


def can_protect(page: dict[str, Any], user: dict[str, Any] | None) -> bool:
    return bool(user) and active() and not page.get("is_home") and pages.can_edit(page, user)


def protect(page: dict[str, Any], user: dict[str, Any]) -> None:
    if not active():
        raise GovernanceError("page_governance.protection.error.disabled")
    if page.get("is_home"):
        raise GovernanceError("page_governance.protection.error.home")
    if not pages.can_edit(page, user):
        raise GovernanceError("page_governance.error.cannot_edit")
    with db.transaction():
        current = _describe(_row(page["id"]), user)
        if current is not None:
            if current["is_controller"]:
                raise GovernanceError("page_governance.protection.error.already_yours")
            raise GovernanceError("page_governance.protection.error.protected_by", user=current["controller_name"])
        pages.set_fields(page["id"], **{**_CLEARED, "protected_by": user["id"], "protected_at": now_sql()})


def unprotect(page: dict[str, Any], user: dict[str, Any]) -> None:
    """The controller hands the page back."""
    with db.transaction():
        current = _describe(_row(page["id"]), user)
        if current is None:
            raise GovernanceError("page_governance.protection.error.not_protected")
        if not current["is_controller"]:
            raise GovernanceError("page_governance.protection.error.not_controller")
        pages.set_fields(page["id"], **_CLEARED)


def request_unlock(page: dict[str, Any], admin: dict[str, Any]) -> bool:
    """Start the unlock delay. Returns True when the page was released at once.

    An administrator who is the controller releases the page immediately.
    Repeated requests keep the original start time.
    """
    if not auth.is_admin(admin):
        raise GovernanceError("page_governance.error.forbidden")
    with db.transaction():
        current = _describe(_row(page["id"]), admin)
        if current is None:
            raise GovernanceError("page_governance.protection.error.not_protected")
        if current["is_controller"]:
            pages.set_fields(page["id"], **_CLEARED)
            return True
        if not current["unlock_requested_at"]:
            pages.set_fields(page["id"], protection_unlock_requested_at=now_sql(),
                             protection_unlock_requested_by=admin["id"])
        return False


def force_unlock(page: dict[str, Any], admin: dict[str, Any]) -> None:
    """Remove the protection once the unlock delay has passed (lapsed protection at once)."""
    if not auth.is_admin(admin):
        raise GovernanceError("page_governance.error.forbidden")
    with db.transaction():
        row = _row(page["id"])
        if row is None or not row["protected_by"]:
            raise GovernanceError("page_governance.protection.error.not_protected")
        current = _describe(row, admin)
        if current is not None and not current["is_controller"]:
            if not current["unlock_requested_at"]:
                raise GovernanceError("page_governance.protection.error.unlock_not_requested")
            if not current["unlock_ready"]:
                raise GovernanceError("page_governance.protection.error.unlock_not_ready",
                                      ready_at=current["unlock_ready_at"])
        pages.set_fields(page["id"], **_CLEARED)


def list_protected() -> list[dict[str, Any]]:
    """Every protected page for the administration screen (lapsed ones included)."""
    rows = db.all(
        "SELECT p.id, p.title, p.slug, p.is_home, p.protected_by, p.protected_at, p.protection_unlock_requested_at, "
        "p.protection_unlock_requested_by, c.username AS controller_name, c.role AS controller_role, "
        "r.username AS requested_by_name "
        "FROM pages p LEFT JOIN users c ON c.id = p.protected_by "
        "LEFT JOIN users r ON r.id = p.protection_unlock_requested_by "
        "WHERE p.protected_by IS NOT NULL ORDER BY p.title COLLATE NOCASE"
    )
    result = []
    for row in rows:
        described = _describe(row, auth.current_user())
        result.append({**row, "state": described, "lapsed": described is None})
    return result
