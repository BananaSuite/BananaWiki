"""HTML this feature adds to other pages through template slots."""

from __future__ import annotations

from typing import Any

from flask import render_template

from ....core.web import allow_form_action
from ... import auth
from . import signin_modes, two_step


def login_below_form(next_url: str = "") -> str:
    """``auth.login.below_form``: the "sign in with the hosting portal" button (the only way in when
    the wiki takes hosting accounts only)."""
    mode = signin_modes.current()
    if mode == signin_modes.LOCAL:
        return ""
    return render_template("auth/_oauth_button.html", next_url=next_url, primary=mode == signin_modes.PLATFORM)


def account_settings_sections() -> str:
    """``account.settings_sections`` (Settings → Security): the one "Sign-in methods" section (BananaWiki
    Hosting, the password, two-step codes, the email address)."""
    from ...registry import is_enabled
    from . import platform_oauth, reauth
    from .routes_oauth import disconnect_refusal

    user = auth.current_user()
    if user is None:
        return ""
    hosting_offered = signin_modes.platform_allowed()
    link = platform_oauth.link_of(user["id"]) if hosting_offered else None
    if hosting_offered and link is None and not auth.on_behalf():
        # The Connect form is answered with a redirect to the portal.
        allow_form_action(platform_oauth.config()["authorize_url"])  # type: ignore[index]
    return render_template(
        "auth/_settings_section.html", user=user, hosting_offered=hosting_offered, link=link,
        disconnect_problem=disconnect_refusal(user) if link else None, has_password=reauth.has_password(user),
        two_step_state=two_step.state(user["id"]), on_behalf=auth.on_behalf(),
        email_offered=is_enabled("attention"),
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
