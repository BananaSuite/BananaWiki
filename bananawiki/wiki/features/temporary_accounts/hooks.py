"""How temporary items show up on pages, profiles and account settings."""

from __future__ import annotations

from typing import Any

from flask import jsonify, redirect, render_template, url_for

from ... import auth
from ...i18n import t
from . import service


def refuse_scheduled_delete(page: dict[str, Any], user: dict[str, Any] | None) -> Any:
    """``page.delete``: a page with an automatic deletion keeps it until the schedule is removed (as in 1.4)."""
    if service.page_schedule(page["id"]) is None:
        return None
    key = "temporary_accounts.error.page_scheduled"
    if auth.wants_json():
        return jsonify({"error": t(key)}), 409
    auth.flash_t(key, "error")
    return redirect(url_for("pages.view", slug=page["slug"]))


def page_countdown(page: dict[str, Any]) -> str:
    deletion = service.page_schedule(page["id"])
    visibility = service.visibility_schedule(page["id"])
    deletion = deletion if deletion and deletion["show_countdown"] else None
    visibility = visibility if visibility and visibility["show_countdown"] else None
    if not deletion and not visibility:
        return ""
    return render_template("temporary_accounts/_page_notice.html", deletion=deletion, visibility=visibility)


def _account_notice(user: dict[str, Any] | None, *, own: bool, only_public: bool) -> str:
    if not user:
        return ""
    deletion = service.account_schedule(user["id"])
    role = service.role_schedule(user["id"])
    if only_public:
        deletion = deletion if deletion and deletion["show_countdown"] else None
        role = role if role and role["show_countdown"] else None
    if not deletion and not role:
        return ""
    return render_template("temporary_accounts/_account_notice.html", deletion=deletion, role=role, own=own,
                           subject=user)


def account_settings_section() -> str:
    return _account_notice(auth.current_user(), own=True, only_public=False)


def profile_section(profile_user: dict[str, Any]) -> str:
    viewer = auth.current_user()
    own = bool(viewer) and viewer["id"] == profile_user["id"]
    return _account_notice(profile_user, own=own, only_public=not (own or auth.is_admin(viewer)))
