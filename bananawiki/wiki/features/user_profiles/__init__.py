"""User profiles: published profile pages, custom profile fields and custom tags.

While this feature is off nobody holds ``profile.view`` or ``profile.edit_own``,
so profiles are visible only to their owners and administrators (as in 1.4,
where the plugin switch hid ``/users``).
"""

from __future__ import annotations

from flask import render_template

from ... import auth
from ...registry import Feature, NavItem
from ..users import service as users
from . import service
from .routes import bp


def _profile_sections(profile_user) -> str:
    viewer = auth.current_user()
    if viewer is None:
        return ""
    privileged = viewer["id"] == profile_user["id"] or auth.is_admin(viewer)
    return render_template(
        "user_profiles/_profile_sections.html", profile_user=profile_user, is_own=viewer["id"] == profile_user["id"],
        fields=service.shown_values(profile_user["id"], include_hidden=privileged),
        tags=service.tags(profile_user["id"]) if privileged else [],
        can_moderate=service.can_moderate(profile_user, viewer),
    )


def _settings_section() -> str:
    user = auth.current_user()
    if not auth.has_permission("profile.edit_own", user):
        return ""
    return render_template("user_profiles/_settings_section.html", profile=users.get_profile(user["id"]) or {},
                           has_fields=bool(service.definitions()))


FEATURE = Feature(
    id="user_profiles",
    name="feature.user_profiles.name",
    description="feature.user_profiles.description",
    toggle="plugin",
    blueprints=[bp],
    nav=[NavItem("profiles.admin.nav", "user_profiles.admin_definitions", icon="id-card", area="admin", order=62,
                 visible=lambda user: bool(user) and auth.is_admin(user))],
    slots={
        "profile.sections": _profile_sections,
        "account.settings_sections": _settings_section,
    },
    order=12,
)
