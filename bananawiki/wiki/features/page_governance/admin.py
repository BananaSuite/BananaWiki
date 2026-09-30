"""Administration: check-outs, reservation quotas, protected pages and governance settings.

1.4 URLs kept: ``/admin/checkouts`` (+ ``/release``, ``/assign``,
``/assign-new``, ``/clear-cooldown``), ``/admin/users/<id>/reservation-quota``
and the page-protection unlock actions under ``/admin/settings/page-protection``
and ``/global-settings/page-protection``.
"""

from __future__ import annotations

from typing import Any

from flask import abort, redirect, render_template, request, url_for

from ....core.web import safe_next
from ... import accounts, auth, settings
from ...db import db
from ..pages import service as pages
from . import protection, quota, reservations
from .errors import GovernanceError
from .messages import flash_error
from .routes import bp, quota_context, requested_quota_from_form

PICKER_LIMIT = 2000

# (column, minimum, maximum) of the numeric settings this page edits.
NUMBER_SETTINGS = (
    ("page_reservation_duration_hours", 1, reservations.MAX_HOURS),
    ("page_reservation_cooldown_hours", 0, reservations.MAX_HOURS),
    ("default_reserved_pages_quota", 1, quota.MAX_QUOTA),
    ("reservation_quota_auto_approve_max", 0, quota.MAX_QUOTA),
    ("quota_request_cooldown_hours", 0, reservations.MAX_HOURS),
)
FLAG_SETTINGS = ("page_protection_enabled", "page_reservations_enabled")


def _page_or_404(page_id: int) -> dict[str, Any]:
    page = pages.get(page_id, with_content=False)
    if page is None:
        abort(404)
    return page


def _to_checkouts():
    return redirect(url_for("page_governance.checkouts"))


# ── Check-outs ────────────────────────────────────────────────────────────────


@bp.get("/admin/checkouts")
@auth.admin_required
def checkouts():
    return render_template(
        "page_governance/admin_checkouts.html",
        reservations=reservations.all_active(),
        cooldowns=reservations.all_cooldowns(),
        quota_requests=quota.all_pending(),
        editors=db.all("SELECT id, username, role FROM users WHERE role IN ('editor', 'admin', 'owner') "
                       "ORDER BY username COLLATE NOCASE LIMIT ?", (PICKER_LIMIT,)),
        page_choices=db.all("SELECT slug, title FROM pages WHERE is_home = 0 ORDER BY title COLLATE NOCASE LIMIT ?",
                            (PICKER_LIMIT,)),
        reservations_active=reservations.active(),
    )


@bp.post("/admin/checkouts/<int:page_id>/release")
@auth.admin_required
def force_release(page_id: int):
    page = _page_or_404(page_id)
    if reservations.force_release(page["id"]):
        auth.flash_t("page_governance.admin.released", "success", title=page["title"])
    else:
        auth.flash_t("page_governance.admin.nothing_to_release", "info")
    return _to_checkouts()


def _assign(page: dict[str, Any] | None):
    if page is None:
        auth.flash_t("page_governance.admin.page_missing", "error")
        return _to_checkouts()
    target = accounts.by_id(request.form.get("user_id")) or accounts.by_username(request.form.get("username"))
    if target is None:
        auth.flash_t("page_governance.admin.user_missing", "error")
        return _to_checkouts()
    try:
        reservations.assign(page, target)
    except GovernanceError as exc:
        flash_error(exc)
    else:
        auth.flash_t("page_governance.admin.assigned", "success", title=page["title"], user=target["username"])
    return _to_checkouts()


@bp.post("/admin/checkouts/<int:page_id>/assign")
@auth.admin_required
def assign(page_id: int):
    return _assign(_page_or_404(page_id))


@bp.post("/admin/checkouts/assign-new")
@auth.admin_required
def assign_new():
    page = pages.get_by_slug((request.form.get("page_slug") or "").strip(), with_content=False)
    if page is None:
        page = pages.get(request.form.get("page_id"), with_content=False)
    return _assign(page)


@bp.post("/admin/checkouts/<int:page_id>/clear-cooldown")
@auth.admin_required
def clear_cooldown(page_id: int):
    page = _page_or_404(page_id)
    count = reservations.clear_cooldowns(page["id"], request.form.get("user_id") or None)
    if count:
        auth.flash_t("page_governance.admin.cooldowns_cleared", "success", count=count, title=page["title"])
    else:
        auth.flash_t("page_governance.admin.no_cooldowns", "info")
    return _to_checkouts()


# ── Reservation quota of one user ────────────────────────────────────────────


@bp.route("/admin/users/<string:user_id>/reservation-quota", methods=["GET", "POST"])
@auth.admin_required
def user_quota(user_id: str):
    target = accounts.by_id(user_id)
    if target is None:
        abort(404)
    if request.method == "POST":
        _handle_admin_quota_form(target)
        return redirect(safe_back(url_for("page_governance.user_quota", user_id=user_id)))
    return render_template("page_governance/admin_quota.html", **quota_context(target, admin_view=True))


def safe_back(default: str) -> str:
    return safe_next(default, request.form.get("next"))


def _handle_admin_quota_form(target: dict[str, Any]) -> None:
    action = request.form.get("action", "")
    try:
        if action == "set_quota":
            quota.set_quota(target["id"], requested_quota_from_form("new_quota"))
            auth.flash_t("page_governance.quota.updated_for", "success", user=target["username"])
        elif action == "reset_quota":
            quota.set_quota(target["id"], None)
            auth.flash_t("page_governance.quota.updated_for", "success", user=target["username"])
        elif action in ("approve_request", "deny_request"):
            row = quota.get_request(request.form.get("request_id", type=int) or 0)
            if row is None or row["user_id"] != target["id"]:
                raise GovernanceError("page_governance.quota.error.missing")
            quota.review(row["id"], auth.current_user()["id"], approve=action == "approve_request",
                         reason=request.form.get("review_reason", ""))
            auth.flash_t("page_governance.quota.approved" if action == "approve_request"
                         else "page_governance.quota.denied", "success")
        else:
            abort(400)
    except GovernanceError as exc:
        flash_error(exc)


# ── Protected pages and settings ─────────────────────────────────────────────


@bp.route("/admin/governance", methods=["GET", "POST"])
@auth.admin_required
def governance():
    if request.method == "POST":
        values = _settings_from_form()
        if values is not None:
            settings.update(values)
            auth.flash_t("common.saved", "success")
        return redirect(url_for("page_governance.governance"))
    return render_template("page_governance/admin_governance.html", protected=protection.list_protected(),
                           settings_values=settings.load(), number_settings=NUMBER_SETTINGS)


def _settings_from_form() -> dict[str, Any] | None:
    values: dict[str, Any] = {name: 1 if request.form.get(name) == "1" else 0 for name in FLAG_SETTINGS}
    for name, minimum, maximum in NUMBER_SETTINGS:
        number = request.form.get(name, type=int)
        if number is None or not minimum <= number <= maximum:
            auth.flash_t("page_governance.admin.invalid_number", "error", minimum=minimum, maximum=maximum)
            return None
        values[name] = number
    return values


@bp.post("/admin/governance/protection/<int:page_id>/request-unlock")
@bp.post("/admin/settings/page-protection/<int:page_id>/request-unlock", endpoint="legacy_request_unlock")
@bp.post("/global-settings/page-protection/<int:page_id>/request-unlock", endpoint="legacy_request_unlock_2")
@auth.admin_required
def request_unlock(page_id: int):
    page = _page_or_404(page_id)
    try:
        released = protection.request_unlock(page, auth.current_user())
    except GovernanceError as exc:
        flash_error(exc)
    else:
        auth.flash_t("page_governance.protection.unprotected" if released
                     else "page_governance.protection.unlock_requested", "success")
    return redirect(url_for("page_governance.governance"))


@bp.post("/admin/governance/protection/<int:page_id>/force-unlock")
@bp.post("/admin/settings/page-protection/<int:page_id>/force-unlock", endpoint="legacy_force_unlock")
@bp.post("/global-settings/page-protection/<int:page_id>/force-unlock", endpoint="legacy_force_unlock_2")
@auth.admin_required
def force_unlock(page_id: int):
    page = _page_or_404(page_id)
    try:
        protection.force_unlock(page, auth.current_user())
    except GovernanceError as exc:
        flash_error(exc)
    else:
        auth.flash_t("page_governance.protection.unprotected", "success")
    return redirect(url_for("page_governance.governance"))
