"""Turning pages into HTML: body, table of contents, author line."""

from __future__ import annotations

import html
import re
from typing import Any

from flask import current_app, url_for
from markupsafe import Markup

from ... import markdown, registry
from ...db import db

_HEADING = re.compile(r'<h([1-4]) id="([^"]+)">(.*?)</h\1>', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
SYSTEM_USER_IDS = frozenset({"-1", "system"})


def body(page: dict[str, Any]) -> Markup:
    """The rendered body of *page*.

    Features may render pages differently (the page builder) through the
    ``page.render`` interceptor; otherwise the Markdown content is used.
    """
    rendered = registry.intercept("page.render", page=page)
    if rendered is not None:
        return Markup(rendered)
    return Markup(markdown.render(page.get("content") or ""))


def table_of_contents(rendered: str) -> list[dict[str, Any]]:
    """Headings of a rendered body: ``[{level, id, text}]``."""
    entries = []
    for level, anchor, inner in _HEADING.findall(rendered):
        text = " ".join(html.unescape(_TAG.sub("", inner)).split())
        if text:
            entries.append({"level": int(level), "id": anchor, "text": text})
    return entries


def profile_url(username: str | None) -> str | None:
    """Link to a user's profile when the profiles feature provides one."""
    if not username or "users.profile" not in current_app.view_functions:
        return None
    try:
        return url_for("users.profile", username=username)
    except Exception:  # noqa: BLE001 - a missing optional route must not break page views
        return None


def editor_name(user_id: str | None) -> str | None:
    """Username for an ``edited_by`` value; None for system edits and removed accounts."""
    if user_id is None or str(user_id) in SYSTEM_USER_IDS:
        return None
    return db.scalar("SELECT username FROM users WHERE id = ?", (str(user_id),))


def last_edit(page: dict[str, Any]) -> dict[str, Any] | None:
    """``{username, profile_url, system, at}`` describing the latest change."""
    if not page.get("last_edited_at"):
        return None
    edited_by = page.get("last_edited_by")
    username = editor_name(edited_by)
    return {
        "username": username,
        "profile_url": profile_url(username),
        "system": edited_by is None or str(edited_by) in SYSTEM_USER_IDS,
        "at": page["last_edited_at"],
    }
