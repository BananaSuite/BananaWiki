"""HTML this feature adds to other pages."""

from __future__ import annotations

from flask import render_template

from ... import auth, settings


def public_notice() -> str:
    """The administrator's message for anonymous visitors while public mode is on."""
    if auth.current_user() is not None or not settings.public_mode_active():
        return ""
    message = str(settings.get("public_mode_message") or "").strip()
    if not settings.get("public_mode_show_message") or not message:
        return ""
    return render_template("site_admin/_public_notice.html", message=message)
