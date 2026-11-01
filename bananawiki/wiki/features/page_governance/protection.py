"""Page protection: one editor controls a page; nobody else may change it.

Protection is on when the feature is enabled and ``page_protection_enabled``
is set. The controller is the editor who protected the page; everyone else,
administrators included, is blocked from editing or deleting it. An
administrator who needs the page back files an unlock request; once
:data:`UNLOCK_DELAY` has passed the administrator may remove the protection.
The controller sees the pending request on the page and can release the page
earlier. Nobody can protect a page that someone else has reserved; an
administrator releases the reservation first.

Protection lapses on its own when the controller's account is deleted
(``ON DELETE SET NULL``) or no longer holds an editing role, and it never
applies to the home page. A lost editing role ends the protections for good
(:func:`end_all`, on ``user.role_changed``): promoting the controller again
does not bring them back.

Each editor protects at most as many pages as their reservation quota
allows (see :mod:`.quota`; counted apart from their reservations), so one
account cannot lock the whole wiki against everyone for the unlock delay.

Switching protection off (``page_protection_enabled`` or the whole feature)
would lift every protection at once, so it is refused while a page is
protected by someone other than the person switching (see
:func:`switch_off_error`); each change of the setting is audited, and so
are an administrator's unlock requests and removals of a protection.

An administrator impersonating the controller is not the controller: the
protection binds them as it binds every administrator, and they cannot
protect pages in the impersonated account's name (see :func:`_impersonated`).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from flask import has_request_context

from ....core.timeutil import now_sql, parse, to_sql, utcnow
from ... import auth, settings
from ...db import db
from ...permissions import EDITOR_ROLES
from ...registry import is_enabled
from ..audit.service import record
from ..pages import service as pages
from . import quota, reservations
from .errors import GovernanceError

UNLOCK_DELAY = timedelta(hours=72)

_CLEARED = {"protected_by": None, "protected_at": None,
            "protection_unlock_requested_at": None, "protection_unlock_requested_by": None}


def active() -> bool:
    return is_enabled("page_governance") and bool(settings.get("page_protection_enabled"))


def switch_off_error(actor: dict[str, Any] | None) -> str | None:
    """Why *actor* may not switch protection off (the setting or the feature), or None.

    Protection binds everyone but its controller, administrators and owners
    included, until the unlock delay; switching it off would skip that delay.
    It is refused while a protection by someone else is in force. The
    server's own commands (no person at the keyboard) may.
    """
    if actor is None or not active():
        return None
    marks = ",".join("?" * len(EDITOR_ROLES))
    others = db.scalar(
        f"SELECT 1 FROM pages p JOIN users c ON c.id = p.protected_by WHERE p.is_home = 0 "
        f"AND p.protected_by != ? AND c.role IN ({marks}) LIMIT 1",
        (actor["id"], *EDITOR_ROLES),
    )
    return "page_governance.protection.error.switch_off_protected" if others else None


def record_switch(enabled: bool) -> None:
    """Audit a change of ``page_protection_enabled`` (call after it changed)."""
    record("page_governance.protection_switched", target_type="setting", target_id="page_protection_enabled",
           details={"enabled": bool(enabled)})


def _impersonated(user: dict[str, Any] | None) -> bool:
    """Whether *user* is the account an administrator is impersonating in this request."""
    if user is None or not has_request_context() or not auth.is_impersonating():
        return False
    current, real = auth.current_user(), auth.real_user()
    return bool(current and real and current["id"] == user["id"] != real["id"])


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
        "is_controller": bool(user) and user["id"] == row["protected_by"] and not _impersonated(user),
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


def _reserved_by_other(page: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any] | None:
    status = reservations.status(page, user)
    return status["reservation"] if status and status["by_other"] else None


def can_protect(page: dict[str, Any], user: dict[str, Any] | None) -> bool:
    return (bool(user) and active() and not page.get("is_home") and not _impersonated(user)
            and pages.can_edit(page, user) and _reserved_by_other(page, user) is None)


def protect(page: dict[str, Any], user: dict[str, Any]) -> None:
    if not active():
        raise GovernanceError("page_governance.protection.error.disabled")
    if page.get("is_home"):
        raise GovernanceError("page_governance.protection.error.home")
    if _impersonated(user):
        raise GovernanceError("page_governance.protection.error.impersonating")
    if not pages.can_edit(page, user):
        raise GovernanceError("page_governance.error.cannot_edit")
    with db.transaction():
        current = _describe(_row(page["id"]), user)
        if current is not None:
            if current["is_controller"]:
                raise GovernanceError("page_governance.protection.error.already_yours")
            raise GovernanceError("page_governance.protection.error.protected_by", user=current["controller_name"])
        # administrators too: the holder would otherwise be locked out until the unlock delay ends
        reservation = _reserved_by_other(page, user)
        if reservation is not None:
            raise GovernanceError("page_governance.protection.error.reserved_by", user=reservation["username"])
        limit = quota.effective(user["id"])
        if limit != quota.UNLIMITED and protected_count(user["id"]) >= limit:
            raise GovernanceError("page_governance.protection.error.quota", quota=limit)
        pages.set_fields(page["id"], **{**_CLEARED, "protected_by": user["id"], "protected_at": now_sql()})


def protected_count(user_id: str) -> int:
    """Pages *user_id* protects (the home page, which protection skips, does not count)."""
    return int(db.scalar("SELECT COUNT(*) FROM pages WHERE protected_by = ? AND is_home = 0", (user_id,), default=0))


def end_all(user_id: str) -> list[str]:
    """End every protection *user_id* holds; the slugs of those pages (home page excluded, it never counts)."""
    with db.transaction():
        slugs = db.column("SELECT slug FROM pages WHERE protected_by = ? AND is_home = 0 ORDER BY slug", (user_id,))
        db.update("pages", _CLEARED, "protected_by = ?", (user_id,))
    return slugs


def unprotect(page: dict[str, Any], user: dict[str, Any]) -> None:
    """The controller hands the page back."""
    with db.transaction():
        current = _describe(_row(page["id"]), user)
        if current is None:
            raise GovernanceError("page_governance.protection.error.not_protected")
        if not current["is_controller"]:
            raise GovernanceError("page_governance.protection.error.not_controller")
        pages.set_fields(page["id"], **_CLEARED)


def _record_unlock(action: str, page: dict[str, Any], controller: str | None, **details: Any) -> None:
    """Audit an administrator's unlock request or removal of a protection (the controller is named)."""
    record(action, target_type="page", target_id=page["id"],
           details={"title": page.get("title"), "controller": controller, **details})


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
            released = True
        else:
            released = False
            if current["unlock_requested_at"]:
                return False
            pages.set_fields(page["id"], protection_unlock_requested_at=now_sql(),
                             protection_unlock_requested_by=admin["id"])
    _record_unlock("page_governance.protection_removed" if released else "page_governance.unlock_requested",
                   page, current["controller_name"])
    return released


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
    _record_unlock("page_governance.protection_removed", page, row["controller_name"],
                   requested_by=row["requested_by_name"], requested_at=row["protection_unlock_requested_at"])


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
