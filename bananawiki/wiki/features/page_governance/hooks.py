"""How page governance plugs into the pages feature: interceptors and slots."""

from __future__ import annotations

from typing import Any

from flask import has_request_context, jsonify, redirect, render_template, request, url_for

from ... import auth
from ...db import db
from ...i18n import t
from ...permissions import EDITOR_ROLES
from ..audit.service import record
from . import protection, reservations
from .errors import GovernanceError
from .messages import error_text


def edit_blocked(page: dict[str, Any], user: dict[str, Any] | None) -> str | None:
    """``page.edit_blocked``: protection by someone else, or a reservation by someone else."""
    if protection.blocks(page, user):
        return "page_governance.blocked.protected"
    if reservations.blocks(page, user):
        return "page_governance.blocked.reserved"
    return None


def refuse_delete(page: dict[str, Any], user: dict[str, Any] | None) -> Any:
    """``page.delete``: the same rules protect a page from deletion."""
    key = edit_blocked(page, user)
    if key is None:
        return None
    if auth.wants_json():
        return jsonify({"error": t(key)}), 423
    auth.flash_t(key, "error")
    return redirect(url_for("pages.view", slug=page["slug"]))


def record_lapsed_protections(user: dict[str, Any], old_role: str = "", new_role: str = "",
                              changed_by: Any = None, **_: Any) -> None:
    """``user.role_changed``: name the protections a lost editing role ends.

    Protection lapses once its controller is no longer an editor (so a page
    is never locked by someone who lost their rights). A demotion may thus
    release a page before the unlock delay; the audit log says which.
    """
    if old_role not in EDITOR_ROLES or new_role in EDITOR_ROLES:
        return
    slugs = db.column("SELECT slug FROM pages WHERE protected_by = ? AND is_home = 0 ORDER BY slug", (user["id"],))
    if slugs:
        record("page_governance.protection_lapsed", actor_id=changed_by, target_type="user", target_id=user["id"],
               details={"username": user.get("username"), "new_role": new_role, "pages": slugs[:50],
                        "count": len(slugs)})


def reserve_after_save(page: dict[str, Any], user: dict[str, Any] | None) -> None:
    """``page.saved``: honour the editor's "reserve this page after saving" box."""
    if not user or not has_request_context() or request.form.get("reserve_after_save") != "1":
        return None
    try:
        reservations.reserve(page, user)
    except GovernanceError as exc:
        auth.flash_t("page_governance.reservation.after_save_failed", "warning", reason=error_text(exc))
    else:
        auth.flash_t("page_governance.reservation.reserved", "success")
    return None


def _context(page: dict[str, Any]) -> dict[str, Any] | None:
    user = auth.current_user()
    if not user or not auth.has_role("editor", user):
        return None
    return {
        "page": page,
        "protection": protection.state(page, user),
        "reservation": reservations.status(page, user),
        "can_protect": protection.can_protect(page, user),
        "can_reserve": reservations.can_reserve(page, user),
    }


def header_actions(page: dict[str, Any]) -> str:
    context = _context(page)
    return render_template("page_governance/_header_actions.html", **context) if context else ""


def above_content(page: dict[str, Any]) -> str:
    user = auth.current_user()
    state = protection.state(page, user) if user else None
    status = reservations.status(page, user) if user and auth.has_role("editor", user) else None
    if not state and not (status and (status["reservation"] or status["cooldown_until"])):
        return ""
    return render_template("page_governance/_notices.html", page=page, protection=state, reservation=status)


def editor_below_form(page: dict[str, Any] | None) -> str:
    user = auth.current_user()
    if page is None or not reservations.can_reserve(page, user):
        return ""
    status = reservations.status(page, user)
    if not status or status["reservation"] or status["cooldown_until"]:
        return ""
    return render_template("page_governance/_editor_option.html")
