"""Difficulty tags: a level (or a custom label and colour) shown on a page.

Stored in ``pages.difficulty_tag`` (``""`` or one of :data:`LEVELS` or
``"custom"``), ``tag_custom_label`` and ``tag_custom_color`` (``#rrggbb``),
exactly as 1.4 did. The custom fields are only kept for the custom tag.

``tag.edit_difficulty`` sets or clears a level; ``tag.edit_custom`` sets a
custom tag (and may clear one). Both also need edit rights on the page.
"""

from __future__ import annotations

import re
from typing import Any

from ... import auth
from ..pages import access as page_access
from ..pages import service as pages

LEVELS = ("beginner", "easy", "intermediate", "expert", "extra")
CUSTOM = "custom"
MAX_LABEL = 50
DEFAULT_COLOR = "#4a90d9"
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class TagError(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


def valid_color(value: str | None) -> bool:
    return bool(value and _HEX.match(value))


def _normalise_color(value: str) -> str:
    value = value.lower()
    if len(value) == 4:
        value = "#" + "".join(c * 2 for c in value[1:])
    return value


def clean(tag: str | None, label: str | None = "", color: str | None = "") -> tuple[str, str, str]:
    """Validate a submitted tag; return ``(tag, label, color)`` ready to store."""
    tag = (tag or "").strip().lower()
    if tag in ("", *LEVELS):
        return tag, "", ""
    if tag != CUSTOM:
        raise TagError("difficulty_tags.error.invalid")
    label = " ".join(_CONTROL.sub(" ", label or "").split())
    if not label:
        raise TagError("difficulty_tags.error.label_required")
    if len(label) > MAX_LABEL:
        raise TagError("difficulty_tags.error.label_too_long", limit=MAX_LABEL)
    color = (color or "").strip()
    if not valid_color(color):
        raise TagError("difficulty_tags.error.color")
    return CUSTOM, label, _normalise_color(color)


def current(page: dict[str, Any]) -> tuple[str, str, str]:
    return (page.get("difficulty_tag") or "", page.get("tag_custom_label") or "", page.get("tag_custom_color") or "")


def permitted_choices(user: dict[str, Any]) -> set[str]:
    """Tag values *user*'s permissions allow (``""`` stands for "no tag")."""
    choices: set[str] = set()
    if auth.has_permission("tag.edit_difficulty", user):
        choices |= {"", *LEVELS}
    if auth.has_permission("tag.edit_custom", user):
        choices |= {"", CUSTOM}
    return choices


def allowed_choices(page: dict[str, Any], user: dict[str, Any] | None = None) -> set[str]:
    """Tag values *user* may set on *page* right now."""
    user = auth.current_user() if user is None else user
    if not user or not pages.can_edit(page, user) or page_access.edit_blocked(page, user):
        return set()
    return permitted_choices(user)


def apply(page: dict[str, Any], user: dict[str, Any], tag: str | None, label: str | None,
          color: str | None, *, choices: set[str] | None = None) -> bool:
    """Set the tag of *page*; return whether it changed. Raises :class:`TagError`.

    *choices* overrides the page check (the creator of a new page may tag it).
    """
    value = clean(tag, label, color)
    if value == current(page):
        return False
    if value[0] not in (allowed_choices(page, user) if choices is None else choices):
        raise TagError("difficulty_tags.error.forbidden")
    pages.set_fields(page["id"], difficulty_tag=value[0], tag_custom_label=value[1], tag_custom_color=value[2])
    return True
