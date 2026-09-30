"""How deletion slowdown plugs into the pages feature."""

from __future__ import annotations

from typing import Any

from flask import jsonify, redirect, render_template, url_for

from ... import accounts, auth
from ...i18n import t
from ..pages import service as pages
from . import service


def schedule_instead(page: dict[str, Any], user: dict[str, Any] | None) -> Any:
    """``page.delete``: mark the page pending deletion instead of deleting it."""
    if page.get("pending_deletion"):
        return _answer(page, "deletion_slowdown.already_pending", "error", 409)
    if service.bypasses(page):
        return None
    service.schedule(page, user["id"] if user else None)
    return _answer(page, "deletion_slowdown.scheduled", "success", 202, hours=service.GRACE_HOURS)


def _answer(page: dict[str, Any], key: str, category: str, status: int, **values: Any) -> Any:
    if auth.wants_json():
        current = pages.get(page["id"], with_content=False) or page
        return jsonify({"status": "pending_deletion", "message": t(key, **values),
                        "purge_at": service.purge_at(current.get("pending_deletion_at"))}), status
    auth.flash_t(key, category, **values)
    return redirect(url_for("pages.view", slug=page["slug"]))


def pending_notice(page: dict[str, Any]) -> str:
    if not page.get("pending_deletion"):
        return ""
    user = auth.current_user()
    current = pages.get(page["id"], with_content=False)
    if current is None:
        return ""
    deleter = accounts.by_id(current.get("pending_deletion_by"))
    return render_template("deletion_slowdown/_notice.html", page=current,
                           deleted_by=deleter["username"] if deleter else None,
                           purge_at=service.purge_at(current["pending_deletion_at"]),
                           can_restore=bool(user) and pages.can_delete(current, user))
