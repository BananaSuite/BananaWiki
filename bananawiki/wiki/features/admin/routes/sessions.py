"""Sign-in sessions: overview, mass sign-out and the daily automatic sign-out."""

from __future__ import annotations

from flask import redirect, render_template, request, url_for

from .... import auth, settings
from .. import service
from ..blueprint import bp
from .common import actor, back


@bp.get("/admin/sessions")
@auth.admin_required
def sessions():
    return render_template(
        "admin/sessions.html",
        sessions=service.active_sessions(),
        current_session_id=auth.current_session_id(),
        auto_logout_enabled=bool(settings.get("auto_logout_enabled")),
        auto_logout_hour=int(settings.get("auto_logout_hour", 0) or 0),
    )


@bp.post("/admin/sessions/auto-logout")
@auth.admin_required
def auto_logout_settings():
    hour = request.form.get("auto_logout_hour", "0")
    hour_value = int(hour) if hour.isdigit() else 0
    settings.update({
        "auto_logout_enabled": 1 if request.form.get("auto_logout_enabled") == "1" else 0,
        "auto_logout_hour": min(max(hour_value, 0), 23),
    })
    auth.flash_t("common.saved", "success")
    return redirect(url_for("admin.sessions"))


@bp.post("/admin/sessions/<string:session_id>/revoke")
@auth.admin_required
def revoke_one_session(session_id: str):
    row = next((s for s in service.active_sessions() if s["id"] == session_id), None)
    if row is not None:
        target = service.get_user(row["user_id"])
        error = service.protection_error(actor(), target) if target else None
        if error:
            auth.flash_t(error, "error")
        else:
            service.revoke_session(actor(), target, session_id)
            auth.flash_t("admin.sessions.revoked", "success", count=1)
    return redirect(url_for("admin.sessions"))


@bp.post("/admin/mass-logout")
@auth.admin_required
def mass_logout():
    count = service.mass_logout(actor())
    auth.flash_t("admin.sessions.mass_logout_done", "success", count=count)
    return back("admin.sessions")
