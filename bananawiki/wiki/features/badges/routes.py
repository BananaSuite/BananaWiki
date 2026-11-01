"""Badge notifications for members and badge administration.

An administrator's changes to badge types and manual awards and revocations
are audited (``badge.*``); automatic awards are not.
"""

from __future__ import annotations

from flask import abort, current_app, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape

from ....core.web import safe_next
from ... import accounts, auth
from ...i18n import t
from ...registry import feature_blueprint
from ..audit.service import record
from . import service

bp = feature_blueprint("badges", "badges", __name__, template_folder="templates", static_folder="static",
                       static_url_path="/static/badges")

HEARTBEAT_INTERVAL = 50
_AUDITED = ("enabled", "auto_trigger", "trigger_type", "trigger_threshold", "allow_multiple")


bp.add_app_template_global(service.chosen_icon, "badge_icon")


@bp.app_template_global("badge_mark")
def badge_mark(badge) -> Markup:
    """The icon an administrator typed, otherwise a dot in the badge's colour (decorative either way)."""
    icon = service.chosen_icon(badge.get("icon"))
    if icon:
        return Markup('<span class="badge-glyph" aria-hidden="true">{}</span>').format(icon)
    color = badge.get("color") or ""
    style = f' style="--badge-color: {escape(color)}"' if service.HEX_COLOR.fullmatch(color) else ""
    return Markup(f'<span class="badge-mark" aria-hidden="true"{style}></span>')


def _audit(action: str, badge: dict, **details) -> None:
    record(action, target_type="badge", target_id=badge["id"], details={"name": badge["name"], **details})


def _badge_or_404(badge_id: int) -> dict:
    badge = service.get_type(badge_id)
    if badge is None:
        abort(404)
    return badge


# Members


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
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"error": t("badges.error.reading_invalid")}), 400
    try:
        seconds = int(data.get("seconds", 0))
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": t("badges.error.reading_invalid")}), 400
    if service.reading_tracked():  # nothing counts reading time otherwise (the page script is not sent either)
        service.add_reading_time(user["id"], seconds)
    return jsonify({"ok": True})


# Administrators


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
        badge_id = service.create_type(values, auth.current_user()["id"])
    except service.BadgeError as error:
        auth.flash_t(error.key, "error", **error.values)
        return render_template("badges/admin_list.html", badges=service.list_types(), triggers=service.TRIGGERS,
                               form=request.form), 400
    _audit("badge.type_created", {**values, "id": badge_id}, **{name: values[name] for name in _AUDITED})
    auth.flash_t("badges.flash.created", "success", name=values["name"])
    return redirect(url_for("badges.admin_list"))


@bp.post("/admin/badges/defaults")
@auth.admin_required
def admin_defaults():
    added = service.add_defaults(auth.current_user()["id"])
    if added:
        record("badge.defaults_added", target_type="badge", details={"count": added})
    auth.flash_t("badges.flash.defaults_added", "success", count=added)
    return redirect(url_for("badges.admin_list"))


@bp.route("/admin/badges/<int:badge_id>/edit", methods=["GET", "POST"])
@auth.admin_required
def admin_edit(badge_id: int):
    badge = _badge_or_404(badge_id)
    if request.method == "POST":
        if request.form.get("action") == "delete":
            holders = len(service.holders(badge_id))
            service.delete_type(badge_id)
            _audit("badge.type_deleted", badge, holders=holders)
            auth.flash_t("badges.flash.deleted", "success")
            return redirect(url_for("badges.admin_list"))
        try:
            values = service.clean_type(request.form)
            service.update_type(badge_id, values)
        except service.BadgeError as error:
            auth.flash_t(error.key, "error", **error.values)
        else:
            changed = sorted(name for name, value in values.items() if badge.get(name) != value)
            if changed:
                _audit("badge.type_updated", {**badge, "name": values["name"]}, changed=", ".join(changed),
                       previous_name=badge["name"], **{name: values[name] for name in _AUDITED})
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
        _audit("badge.awarded", badge, user=target["username"], user_id=target["id"])
        auth.flash_t("badges.flash.awarded", "success", user=target["username"])
    else:
        auth.flash_t("badges.error.already_held", "error", user=target["username"])
    return redirect(url_for("badges.admin_edit", badge_id=badge_id))


@bp.post("/admin/badges/<int:badge_id>/revoke")
@auth.admin_required
def admin_revoke(badge_id: int):
    badge = _badge_or_404(badge_id)
    target = accounts.by_username(request.form.get("username", ""))
    permanent = request.form.get("permanent") == "1"
    if target is None:
        auth.flash_t("badges.error.no_such_user", "error")
    elif count := service.revoke(target["id"], badge_id, auth.current_user()["id"], permanent=permanent):
        _audit("badge.revoked", badge, user=target["username"], user_id=target["id"], permanent=permanent,
               count=count)
        auth.flash_t("badges.flash.revoked", "success", user=target["username"])
    else:
        auth.flash_t("badges.error.not_held", "error", user=target["username"])
    return redirect(url_for("badges.admin_edit", badge_id=badge_id))


@bp.post("/admin/badges/<int:badge_id>/revoke-all")
@auth.admin_required
def admin_revoke_all(badge_id: int):
    badge = _badge_or_404(badge_id)
    permanent = request.form.get("permanent") == "1"
    count = service.revoke_all(badge_id, auth.current_user()["id"], permanent=permanent)
    if count:
        _audit("badge.revoked_all", badge, permanent=permanent, count=count)
    auth.flash_t("badges.flash.revoked_all", "success", count=count)
    return redirect(url_for("badges.admin_edit", badge_id=badge_id))


@bp.post("/admin/badges/evaluate")
@auth.admin_required
def admin_evaluate():
    count = service.evaluate_everyone()
    auth.flash_t("badges.flash.evaluated", "success", count=count)
    return redirect(url_for("badges.admin_list"))
