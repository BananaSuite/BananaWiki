"""HTML this feature adds to other pages through template slots."""

from __future__ import annotations

from typing import Any

from flask import render_template

from ... import auth
from . import platform_oauth, two_step


def login_below_form(next_url: str = "") -> str:
    """``auth.login.below_form``: the "sign in with the hosting portal" button."""
    if platform_oauth.config() is None:
        return ""
    return render_template("auth/_oauth_button.html", next_url=next_url)


def account_settings_sections() -> str:
    """``account.settings_sections``: guided tour, two-step verification and hosting-portal account link."""
    user = auth.current_user()
    if user is None:
        return ""
    impersonating = auth.is_impersonating()
    return render_template(
        "auth/_settings_section.html",
        oauth_enabled=platform_oauth.config() is not None and not impersonating,
        two_step_state=two_step.state(user["id"]), impersonating=impersonating,
    )


def admin_user_detail(target: dict[str, Any] | None = None, **_: Any) -> str:
    """``admin.user_detail``: whether the account uses two-step verification, and its reset."""
    from ..admin import service as admin_service

    viewer = auth.current_user()
    if target is None or viewer is None or not auth.is_admin(viewer):
        return ""
    state = two_step.state(target["id"])
    can_reset = (state is not None and not auth.is_impersonating() and viewer["id"] != target["id"]
                 and admin_service.is_top(viewer) and admin_service.protection_error(viewer, target) is None)
    return render_template("auth/_two_step_admin.html", target=target, state=state,
                           required=two_step.required(target), can_reset=can_reset)
