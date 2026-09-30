"""Badge notifications for members and badge administration."""

from __future__ import annotations

from flask import abort, current_app, jsonify, redirect, render_template, request, url_for

from ....core.web import safe_next
from ... import accounts, auth
from ...i18n import t
from ...registry import feature_blueprint
from . import service

bp = feature_blueprint("badges", "badges", __name__, template_folder="templates", static_folder="static",
                       static_url_path="/static/badges")

HEARTBEAT_INTERVAL = 50


def _badge_or_404(badge_id: int) -> dict:
    badge = service.get_type(badge_id)
    if badge is None:
        abort(404)
    return badge


# ── Members ──────────────────────────────────────────────────────────────────


@bp.get("/badges/notifications")
def notifications():
    return render_template("badges/notifications.html",
                           items=service.unnotified(auth.current_user()["id"]))


@bp.post("/badges/notifications/dismiss")
def dismiss():
    service.dismiss(auth.current_user()["id"])
    auth.flash_t("badges.flash.dismissed", "info")
    return redirect(safe_next(url_for("badges.notifications"), request.form.get("next")))


@bp.post("/badges/reading")
def reading_heartbeat():
    """Seconds spent reading pages, sent by the page script at most once a minute."""
    user = auth.current_user()
    limiter = current_app.extensions["bananawiki.limiter"]
    if not limiter.hit(f"badges:reading:{user['id']}", 1, HEARTBEAT_INTERVAL):
        return jsonify({"error": t("error.429.body")}), 429
    data = request.get_json(silent=True) or {}
    try:
        seconds = int(data.get("seconds", 0))
    except (TypeError, ValueError):
        return jsonify({"error": t("badges.error.reading_invalid")}), 400
    service.add_reading_time(user["id"], seconds)
    return jsonify({"ok": True})


# ── Administrators ───────────────────────────────────────────────────────────


@bp.get("/admin/badges")
@auth.admin_required
def admin_list():
    return render_template("badges/admin_list.html", badges=service.list_types(), triggers=service.TRIGGERS,
                           form={})


@bp.post("/admin/badges/create")
@auth.admin_required
def admin_create():
    try:
        values = service.clean_type(request.form)
        service.create_type(values, auth.current_user()["id"])
    except service.BadgeError as error:
        auth.flash_t(error.key, "error", **error.values)
        return render_template("badges/admin_list.html", badges=service.list_types(), triggers=service.TRIGGERS,
                               form=request.form), 400
    auth.flash_t("badges.flash.created", "success", name=values["name"])
    return redirect(url_for("badges.admin_list"))


@bp.post("/admin/badges/defaults")
@auth.admin_required
def admin_defaults():
    added = service.add_defaults(auth.current_user()["id"])
    auth.flash_t("badges.flash.defaults_added", "success", count=added)
    return redirect(url_for("badges.admin_list"))


@bp.route("/admin/badges/<int:badge_id>/edit", methods=["GET", "POST"])
@auth.admin_required
def admin_edit(badge_id: int):
    badge = _badge_or_404(badge_id)
    if request.method == "POST":
        if request.form.get("action") == "delete":
            service.delete_type(badge_id)
            auth.flash_t("badges.flash.deleted", "success")
            return redirect(url_for("badges.admin_list"))
        try:
            service.update_type(badge_id, service.clean_type(request.form))
        except service.BadgeError as error:
            auth.flash_t(error.key, "error", **error.values)
        else:
            auth.flash_t("badges.flash.saved", "success")
        return redirect(url_for("badges.admin_edit", badge_id=badge_id))
    return render_template("badges/admin_edit.html", badge=badge, triggers=service.TRIGGERS,
                           holders=service.holders(badge_id))


@bp.post("/admin/badges/<int:badge_id>/award")
@auth.admin_required
def admin_award(badge_id: int):
    badge = _badge_or_404(badge_id)
    target = accounts.by_username(request.form.get("username", ""))
    if target is None:
        auth.flash_t("badges.error.no_such_user", "error")
    elif service.award(target["id"], badge, auth.current_user()["id"]):
        auth.flash_t("badges.flash.awarded", "success", user=target["username"])
    else:
        auth.flash_t("badges.error.already_held", "error", user=target["username"])
    return redirect(url_for("badges.admin_edit", badge_id=badge_id))


@bp.post("/admin/badges/<int:badge_id>/revoke")
@auth.admin_required
def admin_revoke(badge_id: int):
    _badge_or_404(badge_id)
    target = accounts.by_username(request.form.get("username", ""))
    if target is None:
        auth.flash_t("badges.error.no_such_user", "error")
    elif service.revoke(target["id"], badge_id, auth.current_user()["id"],
                        permanent=request.form.get("permanent") == "1"):
        auth.flash_t("badges.flash.revoked", "success", user=target["username"])
    else:
        auth.flash_t("badges.error.not_held", "error", user=target["username"])
    return redirect(url_for("badges.admin_edit", badge_id=badge_id))


@bp.post("/admin/badges/<int:badge_id>/revoke-all")
@auth.admin_required
def admin_revoke_all(badge_id: int):
    _badge_or_404(badge_id)
    count = service.revoke_all(badge_id, auth.current_user()["id"], permanent=request.form.get("permanent") == "1")
    auth.flash_t("badges.flash.revoked_all", "success", count=count)
    return redirect(url_for("badges.admin_edit", badge_id=badge_id))


@bp.post("/admin/badges/evaluate")
@auth.admin_required
def admin_evaluate():
    count = service.evaluate_everyone()
    auth.flash_t("badges.flash.evaluated", "success", count=count)
    return redirect(url_for("badges.admin_list"))
