"""Personal data export: a ZIP of everything an account owns."""

from __future__ import annotations

from flask import render_template

from ... import auth
from ...registry import Feature
from .routes import bp


def _settings_section() -> str:
    return render_template("user_data_export/_settings.html")


def _profile_sections(profile_user) -> str:
    viewer = auth.current_user()
    if viewer is None or viewer["id"] == profile_user["id"] or not auth.is_admin(viewer):
        return ""
    if profile_user.get("is_superuser") or profile_user["role"] == "owner":
        return ""
    return render_template("user_data_export/_profile.html", profile_user=profile_user)


FEATURE = Feature(
    id="user_data_export",
    name="feature.user_data_export.name",
    description="feature.user_data_export.description",
    toggle="plugin",
    blueprints=[bp],
    slots={
        "account.settings_sections": _settings_section,
        "profile.sections": _profile_sections,
    },
    order=16,
)
