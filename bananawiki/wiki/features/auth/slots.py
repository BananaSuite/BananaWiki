"""HTML this feature adds to other pages through template slots."""

from __future__ import annotations

from flask import render_template

from ... import auth
from . import platform_oauth


def login_below_form(next_url: str = "") -> str:
    """``auth.login.below_form``: the "sign in with the hosting portal" button."""
    if platform_oauth.config() is None:
        return ""
    return render_template("auth/_oauth_button.html", next_url=next_url)


def account_settings_sections() -> str:
    """``account.settings_sections``: guided tour and hosting-portal account link."""
    user = auth.current_user()
    if user is None:
        return ""
    return render_template(
        "auth/_settings_section.html",
        oauth_enabled=platform_oauth.config() is not None and not auth.is_impersonating(),
    )
