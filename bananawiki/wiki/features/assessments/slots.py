"""HTML the assessments feature adds to wiki pages and account settings."""

from __future__ import annotations

from typing import Any

from flask import render_template

from ... import auth
from . import service
from .routes import points_enabled


def page_header_actions(page: dict[str, Any]) -> str:
    """A "Add quiz" / "Manage quiz" link for the page's managers."""
    if not service.can_manage(page):
        return ""
    return render_template("assessments/_header_action.html", page=page,
                           has_assessment=service.for_page(page["id"]) is not None)


def page_below_content(page: dict[str, Any]) -> str:
    """The "Take the quiz" panel under the page content."""
    assessment = service.for_page(page["id"])
    if assessment is None:
        return ""
    user = auth.current_user()
    context: dict[str, Any] = {"page": page, "assessment": assessment, "user": user, "can_take": False,
                               "can_manage": False, "attempts": [], "block_reason": None}
    if user is not None:
        context["can_take"] = service.can_take(page, user)
        context["can_manage"] = service.can_manage(page, user)
        if not (context["can_take"] or context["can_manage"]):
            return ""
        attempts = service.attempts_of(assessment["id"], user["id"])
        context["attempts"] = attempts
        context["block_reason"] = service.block_reason(assessment, user, attempts=len(attempts))
    context["question_count"] = len(service.questions_for_taker(assessment["id"]))
    return render_template("assessments/_page_panel.html", **context)


def account_settings_section() -> str:
    """Link to the points overview when the administrator enabled it."""
    if auth.current_user() is None or not points_enabled():
        return ""
    return render_template("assessments/_settings_section.html")
