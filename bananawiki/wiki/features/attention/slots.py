"""HTML the attention feature adds to other pages, and the sign-in hook."""

from __future__ import annotations

from typing import Any

from flask import render_template, session

from ... import attention, auth, settings
from . import service

LOGIN_FLAG = "attention_after_login"


def on_login(user: dict[str, Any]) -> None:
    """``user.login``: the first page after signing in summarises what is waiting."""
    session[LOGIN_FLAG] = True


def banners() -> str:
    """``page.banners``: the after-sign-in summary and "your request was decided" notices."""
    user = auth.current_user()
    if user is None or auth.account_block(user):
        return ""
    login_total = attention.total(user) if session.pop(LOGIN_FLAG, None) else 0
    notices = service.notices_for(user["id"])
    if not login_total and not notices:
        return ""
    return render_template("attention/_banners.html", login_total=login_total, notices=notices,
                           notice_text=service.notice_text, notice_url=service.notice_url)


def settings_section() -> str:
    """``account.settings_sections``: notification address and email choices."""
    user = auth.current_user()
    if user is None:
        return ""
    return render_template("attention/_settings_section.html", user=user,
                           reviewer=auth.has_role("editor", user), mail_ready=service.mail_configured())


def account_status(state: str, user: dict[str, Any]) -> str:
    """``auth.account_status``: someone waiting for approval can leave an address to hear back."""
    if state != "pending" or auth.is_impersonating() or not settings.get("decision_email_enabled") \
            or not service.mail_configured():
        return ""
    return render_template("attention/_status_email.html", user=user)
