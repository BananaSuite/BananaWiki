"""The announcement bar rendered into the ``page.banners`` slot."""

from __future__ import annotations

from typing import Any

from flask import render_template
from markupsafe import Markup

from ... import auth, settings
from ...markdown import render as render_markdown
from . import service


def _bar_item(row: dict[str, Any], *, can_open: bool) -> dict[str, Any]:
    content = row["content"]
    truncated = len(content) > service.BAR_EXCERPT and can_open
    if truncated:
        content = content[: service.BAR_EXCERPT - 10].rstrip() + "…"
    return {**row, "html": Markup(render_markdown(content)), "truncated": truncated,
            "style": service.custom_style(row), "expires_iso": service.expires_iso(row)}


def render_bar() -> str:
    """The announcements the current visitor should see, above every page."""
    user = auth.current_user()
    rows = service.visible_for(user)
    if not rows:
        return ""
    # Anonymous visitors can open the full announcement only while public mode is on;
    # otherwise the bar shows the whole text.
    can_open = user is not None or settings.public_mode_active()
    return render_template("announcements/_bar.html", items=[_bar_item(row, can_open=can_open) for row in rows],
                           viewer_id=user["id"] if user else "guest")
