"""How contributions plug into the pages feature."""

from __future__ import annotations

from typing import Any

from flask import redirect, render_template, url_for

from ... import auth
from . import service


def offer_proposal(page: dict[str, Any], user: dict[str, Any] | None) -> Any:
    """``page.edit_denied``: send readers who may propose to the proposal form."""
    if auth.wants_json() or not service.can_propose(page, user):
        return None
    auth.flash_t("contributions.redirected_to_proposal", "info")
    return redirect(url_for("contributions.propose", slug=page["slug"]))


def header_actions(page: dict[str, Any]) -> str:
    user = auth.current_user()
    if not service.can_propose(page, user):
        return ""
    return render_template("contributions/_header_actions.html", page=page,
                           own=service.own_pending(page["id"], user["id"]))


def above_content(page: dict[str, Any]) -> str:
    user = auth.current_user()
    if not user:
        return ""
    own = service.own_pending(page["id"], user["id"])
    waiting = service.pending_on_page(page["id"]) if service.can_review(page, user) else []
    if not own and not waiting:
        return ""
    return render_template("contributions/_notices.html", page=page, own=own, waiting=waiting)
