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
* Members may block other members (:mod:`.blocks`): no direct messages
  between them, and the blocked member's group messages are hidden from the
  one who blocked them. They unblock in their account settings.
"""

from __future__ import annotations

from typing import Any

from markupsafe import Markup

from ... import attention, auth
from ...registry import AttentionSource, Feature, Job, NavItem
from . import (  # noqa: F401
    badges,
    blocks,
    dms,
    groups,
    policy,
    reports,
    retention,
    routes_admin,
    routes_blocks,
    routes_dm,
    routes_groups,
)
from .web import bp


def _dm_unread(user: dict[str, Any] | None) -> int:
    return dms.unread_total(user["id"]) if user and policy.dm_allowed(user) else 0


def _group_unread(user: dict[str, Any] | None) -> int:
    return groups.unread_total(user["id"]) if user and policy.group_allowed(user) else 0


def _settings_sections(**context: Any) -> Markup:
    """Slot ``account.settings_sections``: groups on the profile, then the people the user blocked."""
    return Markup(badges.render_settings_section(**context)) + Markup(blocks.render_settings_section(**context))


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
        NavItem("chat.nav.groups", "chat.group_list", icon="messages", area="apps", order=41,
                visible=lambda user: bool(user) and policy.group_allowed(user), badge=_group_unread),
        NavItem("chat.nav.admin", "chat.admin_chats", icon="message", area="admin", order=60,
                visible=lambda user: bool(user) and auth.is_admin(user), active_endpoints=("chat.admin_groups",),
                badge=attention.badge("chat.reports")),
    ],
    # Open reports are a queue for administrators: they count in the avatar's red total, the
    # "Needs your attention" menu and page, and the attention emails, like the other queues.
    attention=[AttentionSource("chat.reports", "attention.source.chat.reports", "chat.admin_reports",
                               lambda user: reports.open_count() if auth.is_admin(user) else 0, order=60)],
    jobs=[
        Job("chat.retention", 3600, retention.retention_job),
        Job("chat.housekeeping", 86400, retention.housekeeping_job, initial_delay=600),
    ],
    slots={
        "profile.sections": badges.render_profile_section,
        "account.settings_sections": _settings_sections,
    },
)
