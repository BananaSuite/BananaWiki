"""Route: /signup (invite codes, open sign-up, approval, bot protection, per-address limit)."""

from __future__ import annotations

from flask import redirect, render_template, request, url_for

from ....core.ratelimit import SqlLimiter
from ....core.web import client_ip
from ... import accounts, auth, settings
from ...db import db
from ...i18n import t
from . import bot_protection, invites, signup
from .blueprint import bp

WINDOW_SECONDS = 3600
MAX_PER_IP = 10


def _render(status: int = 200, error: str | None = None, form: dict | None = None):
    return render_template(
        "auth/signup.html",
        error=error,
        form=form or {},
        invite_required=signup.invite_required(),
        approval_required=settings.approval_required(),
        bot_protection=bot_protection.enabled(),
        form_token=bot_protection.new_token(),
        invite=invites.normalize(request.args.get("code")),
    ), status


@bp.route("/signup", methods=["GET", "POST"], endpoint="signup")
@auth.public
def signup_page():
    if auth.current_user() is not None:
        return redirect("/")
    if not signup.signup_available():
        return redirect(url_for("auth.maintenance") if settings.maintenance_active() else url_for("auth.login"))
    if request.method == "GET":
        return _render()

    form = {"username": (request.form.get("username") or "").strip()[:50],
            "invite_code": invites.normalize(request.form.get("invite_code"))}
    if not SqlLimiter(db.session).hit(client_ip(), "signup:ip", MAX_PER_IP, WINDOW_SECONDS):
        return _render(429, t("auth.signup.error.too_many"), form)
    if bot_protection.rejection():
        return _render(400, t("auth.signup.error.rejected"), form)
    password = request.form.get("password") or ""
    if password != request.form.get("confirm_password", ""):
        return _render(400, t("auth.error.passwords_differ"), form)
    try:
        user = signup.register(form["username"], password, invite_code=form["invite_code"])
    except accounts.AccountError as exc:
        return _render(400, t(exc.key, **exc.values), form)
    pending = user["approval_status"] == "pending"
    auth.flash_t("auth.signup.pending" if pending else "auth.signup.done", "success")
    return redirect(url_for("auth.login"))
