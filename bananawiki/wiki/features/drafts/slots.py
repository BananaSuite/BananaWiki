"""The drafts panel in the page editor and the link in account settings."""

from __future__ import annotations

from typing import Any

from flask import render_template, request

from ....core.timeutil import parse
from ... import auth
from ..pages.blueprint import page_url
from . import service


def editor_panel(page: dict[str, Any] | None) -> str:
    """Autosave status, the user's own draft and other editors' drafts (``editor.below_form``)."""
    user = auth.current_user()
    if page is None or user is None or not service.can_edit(page, user):
        return ""
    can_save = auth.has_permission("draft.create", user)
    draft = service.get(page["id"], user["id"]) if auth.has_permission("draft.view_own", user) else None
    others = service.others(page["id"], user["id"])
    if not (can_save or draft or others):
        return ""
    edited, saved = parse(page.get("last_edited_at")), parse(draft["updated_at"]) if draft else None
    return render_template(
        "drafts/_editor_panel.html", page=page, draft=draft, others=others, can_save=can_save,
        can_delete=auth.has_permission("draft.delete_own", user),
        can_transfer=service.can_transfer(page, user),
        stale=bool(draft and edited and saved and edited > saved),
        # After a conflict the editor already holds the user's text: never offer to overwrite it.
        offer_restore=bool(draft) and request.method == "GET",
        close_url=page_url(page),
    )


def settings_section() -> str:
    """Link to "My drafts" in the account settings (``account.settings_sections``)."""
    user = auth.current_user()
    if user is None or not auth.has_permission("draft.view_own", user):
        return ""
    return render_template("drafts/_settings_section.html", count=service.count_of_user(user["id"]))
