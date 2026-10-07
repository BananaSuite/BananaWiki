"""People: account settings, sessions, display preferences, profiles and account merges.

Slots filled here
-----------------
* ``page.scripts`` - the viewer's finer display preferences (exact text size,
  colours, background picture) as CSS, and the script that saves the theme
  switch on the account.
* ``auth.account_status`` - suspended administrators may lift their own suspension.
"""

from __future__ import annotations

from flask import render_template

from ... import attention, auth
from ...registry import AttentionSource, Feature, Job, NavItem
from . import merge, merge_routes, preferences, service  # noqa: F401 - merge_routes registers views
from .routes import bp


def _can_search_people(user) -> bool:
    return bool(user) and auth.has_permission("search.users", user)


def _is_admin(user) -> bool:
    return bool(user) and auth.is_admin(user)


def _merges_awaiting(user) -> int:
    return merge.awaiting_admin_count() if _is_admin(user) else 0


def _display_css() -> str:
    user = auth.current_user()
    prefs = preferences.current(user)
    css = preferences.custom_css(prefs, service.upload_url(prefs.get("background_image")) if user else "")
    return render_template("users/_display_slot.html", css=css)


def _account_status(state: str, user) -> str:
    if state != "suspended" or not service.may_reactivate_self(user):
        return ""
    return render_template("users/_suspended_actions.html")


FEATURE = Feature(
    id="users",
    name="feature.users.name",
    description="feature.users.description",
    toggle="always",
    blueprints=[bp],
    nav=[
        NavItem("users.nav.people", "users.members", icon="users", area="apps", order=40,
                visible=_can_search_people),
        NavItem("users.nav.profile", "users.my_profile", icon="user", area="user", order=10),
        NavItem("users.nav.settings", "users.settings", icon="gear", area="user", order=20),
        NavItem("users.nav.merge_requests", "users.admin_merge_requests", icon="merge", area="admin", order=60,
                visible=_is_admin, badge=attention.badge("users.merge_requests")),
    ],
    attention=[AttentionSource("users.merge_requests", "attention.source.users.merge_requests",
                               "users.admin_merge_requests", _merges_awaiting,
                               oldest=lambda user: merge.awaiting_admin_oldest(), order=40)],
    jobs=[Job("users.collect_orphan_images", 86400, service.collect_orphan_images, initial_delay=600)],
    slots={
        "page.scripts": _display_css,
        "auth.account_status": _account_status,
    },
    order=10,
)
