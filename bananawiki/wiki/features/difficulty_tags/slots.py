"""Where difficulty tags appear: above the page text, in the page header and in the editor."""

from __future__ import annotations

from typing import Any

from flask import render_template

from ... import auth
from . import service


def _form_context(page: dict[str, Any] | None, choices: set[str]) -> dict[str, Any]:
    tag, label, color = service.current(page) if page else ("", "", "")
    return {"page": page, "levels": [lv for lv in service.LEVELS if lv in choices],
            "allow_custom": service.CUSTOM in choices, "tag": tag, "label": label,
            "color": color or service.DEFAULT_COLOR, "max_label": service.MAX_LABEL}


def badge(page: dict[str, Any]) -> str:
    """The tag itself (``page.above_content``)."""
    tag, label, color = service.current(page)
    if not tag or (tag == service.CUSTOM and not (label and service.valid_color(color))):
        return ""
    return render_template("difficulty_tags/_badge.html", tag=tag, label=label, color=color)


def header_action(page: dict[str, Any]) -> str:
    """"Tag" button and dialog for people who may change the tag (``page.header_actions``)."""
    choices = service.allowed_choices(page)
    if not choices:
        return ""
    return render_template("difficulty_tags/_dialog.html", **_form_context(page, choices))


def editor_fields(page: dict[str, Any] | None) -> str:
    """Tag fields inside the page editor and the "new page" form (``editor.below_form``)."""
    user = auth.current_user()
    if user is None:
        return ""
    choices = service.permitted_choices(user) if page is None else service.allowed_choices(page, user)
    if not choices:
        return ""
    return render_template("difficulty_tags/_fields.html", prefix="editor", **_form_context(page, choices))
