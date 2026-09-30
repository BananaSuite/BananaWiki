"""Group badges on profiles (``profile_group_badges``).

When the administrator enables ``profile_group_badges_enabled``, members may
choose which of their groups appear on their profile. The choice is private
by default and is forgotten when they leave or are removed from the group.
"""

from __future__ import annotations

from typing import Any

from flask import render_template
from markupsafe import Markup

from ....core.timeutil import now_sql
from ... import auth, settings
from ...db import db
from . import policy


def enabled() -> bool:
    return bool(settings.get("profile_group_badges_enabled"))


def visible_groups(user_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT gc.id, gc.name, gc.is_global FROM profile_group_badges b "
        "JOIN group_members gm ON gm.group_id = b.group_id AND gm.user_id = b.user_id AND gm.banned = 0 "
        "JOIN group_chats gc ON gc.id = b.group_id "
        "WHERE b.user_id = ? AND b.visible = 1 ORDER BY gc.name COLLATE NOCASE",
        (user_id,),
    )


def choices(user_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT gc.id, gc.name, gc.is_global, COALESCE(b.visible, 0) AS visible FROM group_members gm "
        "JOIN group_chats gc ON gc.id = gm.group_id "
        "LEFT JOIN profile_group_badges b ON b.group_id = gm.group_id AND b.user_id = gm.user_id "
        "WHERE gm.user_id = ? AND gm.banned = 0 ORDER BY gc.name COLLATE NOCASE",
        (user_id,),
    )


def set_visible(user_id: str, group_id: int, visible: bool) -> None:
    db.execute(
        "INSERT INTO profile_group_badges (user_id, group_id, visible, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, group_id) DO UPDATE SET visible = excluded.visible, updated_at = excluded.updated_at",
        (user_id, group_id, 1 if visible else 0, now_sql()),
    )


def render_profile_section(profile_user: dict[str, Any] | None = None, **_context: Any) -> Markup | str:
    """Slot ``profile.sections``: the groups the profile owner chose to show."""
    if not profile_user or not enabled():
        return ""
    groups = visible_groups(profile_user["id"])
    if not groups:
        return ""
    return Markup(render_template("chat/_profile_groups.html", groups=groups))


def render_settings_section(**_context: Any) -> Markup | str:
    """Slot ``account.settings_sections``: choose which groups appear on the profile."""
    user = auth.current_user()
    if not user or not enabled() or not policy.group_allowed(user):
        return ""
    return Markup(render_template("chat/_settings_groups.html", groups=choices(user["id"])))
