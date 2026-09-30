"""Messaging: direct messages and group chats (the 1.4 "chat" plugin).

* Direct messages at ``/chats`` and group chats at ``/groups`` (invite codes,
  the global room, owner/moderator/member roles, timeouts, bans), with file
  attachments, exports and clearing.
* Chat pages load the latest messages, then fetch only newer ones (JSON,
  rendered by ``static/chat.js``), page backwards on demand and slow down
  polling while the tab is hidden.
* Deleting a message erases its text and attachments for everyone.
* Administrators monitor conversations and groups and configure limits and
  retention at ``/admin/chats``; retention runs as the ``chat.retention`` job.
* Members may show their groups on their profile (``profile.sections``).
"""

from __future__ import annotations

from typing import Any

from ... import auth
from ...registry import Feature, Job, NavItem
from . import badges, dms, groups, policy, retention, routes_admin, routes_dm, routes_groups  # noqa: F401
from .web import bp


def _dm_unread(user: dict[str, Any] | None) -> int:
    return dms.unread_total(user["id"]) if user and policy.dm_allowed(user) else 0


def _group_unread(user: dict[str, Any] | None) -> int:
    return groups.unread_total(user["id"]) if user and policy.group_allowed(user) else 0


FEATURE = Feature(
    id="chat",
    name="feature.chat.name",
    description="feature.chat.description",
    toggle="plugin",
    default_enabled=True,
    easy_wiki=False,
    blueprints=[bp],
    nav=[
        NavItem("chat.nav.messages", "chat.dm_list", icon="message", area="apps", order=40,
                visible=lambda user: bool(user) and policy.dm_allowed(user), badge=_dm_unread),
        NavItem("chat.nav.groups", "chat.group_list", icon="users", area="apps", order=41,
                visible=lambda user: bool(user) and policy.group_allowed(user), badge=_group_unread),
        NavItem("chat.nav.admin", "chat.admin_chats", icon="message", area="admin", order=60,
                visible=lambda user: bool(user) and auth.is_admin(user)),
    ],
    jobs=[
        Job("chat.retention", 3600, retention.retention_job),
        Job("chat.housekeeping", 86400, retention.housekeeping_job, initial_delay=600),
    ],
    slots={
        "profile.sections": badges.render_profile_section,
        "account.settings_sections": badges.render_settings_section,
    },
)
