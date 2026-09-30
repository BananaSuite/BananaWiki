"""Editor-facing routes: reservation directory, check-out, protection and quota requests.

URLs are the 1.4 ones: ``/reservations``, ``/page/<slug>/reserve``,
``/page/<slug>/reservation/release``, ``/page/<slug>/protection``,
``/settings/reservation-quota`` and the JSON endpoints under
``/api/pages/<id>/reservation``.
"""

from __future__ import annotations

from typing import Any

from flask import abort, jsonify, redirect, render_template, request, url_for

from ....core.web import safe_next
from ... import auth
from ...db import db
from ...i18n import t
from ...registry import feature_blueprint
from ..pages import service as pages
from . import protection, quota, reservations
from .errors import GovernanceError
from .messages import error_text, flash_error

bp = feature_blueprint("page_governance", "page_governance", __name__, template_folder="templates")

DIRECTORY_PAGE_SIZE = 100


def visible_page_or_404(slug: str) -> dict[str, Any]:
    page = pages.get_by_slug(slug, with_content=False)
    if page is None or not pages.can_view(page):
        abort(404)
    return page


def back_to(page: dict[str, Any]) -> Any:
    return redirect(safe_next(url_for("pages.view", slug=page["slug"]), request.form.get("next")))


# ── Reservation directory ─────────────────────────────────────────────────────


@bp.get("/reservations")
@auth.editor_required
def directory():
    if not reservations.active():
        auth.flash_t("page_governance.reservation.error.disabled", "error")
        return redirect(url_for("pages.home"))
    user = auth.current_user()
    editable = [p for p in pages.list_visible(user=user, include_home=False)
                if not p["pending_deletion"] and pages.can_edit(p, user)]
    total_pages = max(1, -(-len(editable) // DIRECTORY_PAGE_SIZE))
    current = min(max(1, request.args.get("page", 1, type=int)), total_pages)
    shown = editable[(current - 1) * DIRECTORY_PAGE_SIZE:current * DIRECTORY_PAGE_SIZE]
    states = reservations.statuses([p["id"] for p in shown], user["id"])
    categories = {row["id"]: row["name"] for row in db.all("SELECT id, name FROM categories")}
    rows = [{"page": p, "category": categories.get(p["category_id"]), **states[p["id"]]} for p in shown]
    return render_template(
        "page_governance/reservations.html", rows=rows, page_number=current, total_pages=total_pages,
        quota=quota.effective(user["id"]), active_count=reservations.active_count(user["id"]),
    )


# ── Check-out and protection ─────────────────────────────────────────────────


@bp.post("/page/<slug>/reserve")
@auth.editor_required
def reserve(slug: str):
    page = visible_page_or_404(slug)
    try:
        reservations.reserve(page, auth.current_user())
    except GovernanceError as exc:
        flash_error(exc)
    else:
        auth.flash_t("page_governance.reservation.reserved", "success")
    return back_to(page)


@bp.post("/page/<slug>/reservation/release")
@auth.editor_required
def release(slug: str):
    page = visible_page_or_404(slug)
    if reservations.release(page, auth.current_user()):
        auth.flash_t("page_governance.reservation.released", "success")
    else:
        auth.flash_t("page_governance.reservation.error.not_holder", "error")
    return back_to(page)


@bp.post("/page/<slug>/protection")
@auth.editor_required
def update_protection(slug: str):
    page = visible_page_or_404(slug)
    user = auth.current_user()
    action = request.form.get("action", "")
    try:
        if action == "protect":
            protection.protect(page, user)
            auth.flash_t("page_governance.protection.protected", "success")
        elif action == "unprotect":
            protection.unprotect(page, user)
            auth.flash_t("page_governance.protection.unprotected", "success")
        else:
            abort(400)
    except GovernanceError as exc:
        flash_error(exc)
    return back_to(page)


# ── JSON API (session authenticated, as in 1.4) ─────────────────────────────


def _api_page(page_id: int) -> dict[str, Any]:
    if not reservations.active():
        abort(jsonify({"error": t("page_governance.reservation.error.disabled")}), 403)
    page = pages.get(page_id, with_content=False)
    if page is None or not pages.can_view(page):
        abort(jsonify({"error": t("error.404.title")}), 404)
    if page["is_home"]:
        abort(jsonify({"error": t("page_governance.reservation.error.home")}), 400)
    if not pages.can_edit(page):
        abort(jsonify({"error": t("page_governance.error.cannot_edit")}), 403)
    return page


def _status_json(page: dict[str, Any]) -> dict[str, Any]:
    state = reservations.status(page, auth.current_user()) or {}
    reservation = state.get("reservation")
    return {
        "is_reserved": bool(reservation),
        "reserved_by": reservation["user_id"] if reservation else None,
        "reserved_by_username": reservation["username"] if reservation else None,
        "reserved_at": reservation["reserved_at"] if reservation else None,
        "expires_at": reservation["expires_at"] if reservation else None,
        "user_in_cooldown": bool(state.get("cooldown_until")),
        "cooldown_until": state.get("cooldown_until"),
    }


@bp.get("/api/pages/<int:page_id>/reservation/status")
@auth.editor_required
def api_status(page_id: int):
    return jsonify(_status_json(_api_page(page_id)))


@bp.post("/api/pages/<int:page_id>/reservation")
@auth.editor_required
def api_reserve(page_id: int):
    page = _api_page(page_id)
    try:
        row = reservations.reserve(page, auth.current_user())
    except GovernanceError as exc:
        return jsonify({"error": error_text(exc)}), 409
    return jsonify({"ok": True, "message": t("page_governance.reservation.reserved"), "reservation": {
        "page_id": row["page_id"], "reserved_at": row["reserved_at"], "expires_at": row["expires_at"]}})


@bp.delete("/api/pages/<int:page_id>/reservation")
@auth.editor_required
def api_release(page_id: int):
    page = _api_page(page_id)
    if not reservations.release(page, auth.current_user()):
        return jsonify({"error": t("page_governance.reservation.error.not_holder")}), 404
    return jsonify({"ok": True, "message": t("page_governance.reservation.released")})


# ── Reservation quota (the editor's own) ─────────────────────────────────────


@bp.get("/account/reservation-quota")
@auth.editor_required
def legacy_quota():
    return redirect(url_for("page_governance.my_quota"), code=301)


@bp.route("/settings/reservation-quota", methods=["GET", "POST"])
@auth.editor_required
def my_quota():
    user = auth.current_user()
    if request.method == "POST":
        _handle_own_quota_form(user)
        return redirect(url_for("page_governance.my_quota"))
    return render_template("page_governance/quota.html", **quota_context(user, admin_view=False))


def _handle_own_quota_form(user: dict[str, Any]) -> None:
    action = request.form.get("action", "")
    try:
        if action == "set_quota" and auth.is_admin(user):
            quota.set_quota(user["id"], requested_quota_from_form("new_quota"))
            auth.flash_t("page_governance.quota.updated", "success")
        elif action == "submit_quota_request" and not auth.is_admin(user):
            row = quota.create_request(user["id"], requested_quota_from_form("requested_quota"),
                                       request.form.get("reason", ""))
            auth.flash_t("page_governance.quota.auto_approved" if row["status"] == "approved"
                         else "page_governance.quota.submitted", "success")
        elif action == "cancel_request" and not auth.is_admin(user):
            quota.cancel(request.form.get("request_id", type=int) or 0, user["id"])
            auth.flash_t("page_governance.quota.cancelled", "success")
        else:
            abort(400)
    except GovernanceError as exc:
        flash_error(exc)


def requested_quota_from_form(field: str) -> int:
    if request.form.get("unlimited") == "1":
        return quota.UNLIMITED
    return quota.normalize(request.form.get(field, ""))


def quota_context(target: dict[str, Any], *, admin_view: bool) -> dict[str, Any]:
    return {
        "target_user": target,
        "admin_view": admin_view,
        "current_quota": quota.effective(target["id"]),
        "default_quota": quota.default_quota(),
        "active_count": reservations.active_count(target["id"]),
        "reservations": reservations.of_user(target["id"]),
        "pending_request": quota.pending_request(target["id"]),
        "requests": quota.history(target["id"]),
        "cooldown_minutes": quota.cooldown_remaining_minutes(target["id"]),
        "max_reason": quota.MAX_REASON,
        "max_review_reason": quota.MAX_REVIEW_REASON,
        "reservations_active": reservations.active(),
    }
