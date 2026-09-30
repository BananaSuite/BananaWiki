"""Routes for proposing, managing and reviewing contributions (1.4 URLs)."""

from __future__ import annotations

from typing import Any

from flask import abort, flash, redirect, render_template, request, url_for

from ... import accounts, auth, settings
from ...i18n import t
from ...registry import feature_blueprint
from ..pages import service as pages
from . import quota, service
from .errors import ContributionError

bp = feature_blueprint("contributions", "contributions", __name__, template_folder="templates")

NUMBER_SETTINGS = (
    ("default_contribution_quota", 1, quota.MAX_QUOTA),
    ("contribution_quota_auto_approve_max", 0, quota.MAX_QUOTA),
    ("quota_request_cooldown_hours", 0, 24 * 365),
)


def flash_error(exc: ContributionError) -> None:
    flash(t(exc.key, **exc.values), "error")


def _visible_page(slug: str) -> dict[str, Any]:
    page = pages.get_by_slug(slug)
    if page is None or not pages.can_view(page):
        abort(404)
    return page


def _own_contribution(slug: str, contribution_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """The caller's contribution *contribution_id*, which must belong to page *slug*."""
    page = _visible_page(slug)
    contribution = service.get(contribution_id)
    if contribution is None or contribution["page_id"] != page["id"]:
        abort(404)
    if contribution["user_id"] != auth.current_user()["id"]:
        abort(403)
    return page, contribution


def _to_page(page: dict[str, Any]):
    return redirect(url_for("pages.view", slug=page["slug"]))


# ── Proposing ────────────────────────────────────────────────────────────────


@bp.route("/page/<slug>/propose-edit", methods=["GET", "POST"])
def propose(slug: str):
    page = _visible_page(slug)
    user = auth.current_user()
    if pages.can_edit(page, user):
        return redirect(url_for("pages.edit", slug=slug))
    if not service.can_propose(page, user):
        return auth.deny()
    existing = service.own_pending(page["id"], user["id"])
    if existing:
        return redirect(url_for("contributions.edit", slug=slug, contribution_id=existing["id"]))
    form = {"title": page["title"], "content": page["content"], "reason": ""}
    if request.method == "POST":
        form = {key: request.form.get(key, "") for key in form}
        try:
            service.propose(page, user, **form)
        except ContributionError as exc:
            flash_error(exc)
        else:
            auth.flash_t("contributions.submitted", "success")
            return _to_page(page)
    return render_template("contributions/propose.html", page=page, form=form, contribution=None,
                           others=len(service.pending_on_page(page["id"])), max_reason=service.MAX_REASON)


@bp.route("/page/<slug>/contribution/<int:contribution_id>/edit", methods=["GET", "POST"])
def edit(slug: str, contribution_id: int):
    page, contribution = _own_contribution(slug, contribution_id)
    user = auth.current_user()
    if contribution["status"] != "pending":
        auth.flash_t("contributions.error.not_pending", "error")
        return _to_page(page)
    if pages.can_edit(page, user):
        service.withdraw(contribution, user)
        auth.flash_t("contributions.withdrawn_can_edit", "info")
        return redirect(url_for("pages.edit", slug=slug))
    form = {key: contribution[key] for key in ("title", "content", "reason")}
    if request.method == "POST":
        form = {key: request.form.get(key, "") for key in form}
        try:
            service.update_own(contribution, user, **form)
        except ContributionError as exc:
            flash_error(exc)
        else:
            auth.flash_t("contributions.updated", "success")
            return _to_page(page)
    return render_template("contributions/propose.html", page=page, form=form, contribution=contribution,
                           others=0, max_reason=service.MAX_REASON)


@bp.post("/page/<slug>/contribution/<int:contribution_id>/withdraw")
def withdraw(slug: str, contribution_id: int):
    page, contribution = _own_contribution(slug, contribution_id)
    try:
        service.withdraw(contribution, auth.current_user())
    except ContributionError as exc:
        flash_error(exc)
    else:
        auth.flash_t("contributions.withdrawn", "success")
    return _to_page(page)


# ── The proposer's own list and quota ───────────────────────────────────────


@bp.get("/my-contributions")
def mine():
    user = auth.current_user()
    return render_template(
        "contributions/mine.html", contributions=service.of_user(user["id"]), quota=quota.effective(user["id"]),
        pending=service.pending_count(user["id"]), pending_request=quota.pending_request(user["id"]),
        requests=quota.history(user["id"]), cooldown_minutes=quota.cooldown_remaining_minutes(user["id"]),
        can_request=_may_request_quota(user), max_reason=quota.MAX_REASON,
    )


def _may_request_quota(user: dict[str, Any]) -> bool:
    return auth.has_permission("contribution.propose", user) and not auth.has_permission("page.edit_all", user)


@bp.post("/my-contributions/quota-request")
def quota_request():
    user = auth.current_user()
    if not _may_request_quota(user):
        return auth.deny()
    try:
        requested = quota.UNLIMITED if request.form.get("unlimited") == "1" else request.form.get("requested_quota")
        row = quota.create_request(user["id"], requested, request.form.get("reason", ""))
    except ContributionError as exc:
        flash_error(exc)
    else:
        auth.flash_t("contributions.quota.auto_approved" if row["status"] == "approved"
                     else "contributions.quota.submitted", "success")
    return redirect(url_for("contributions.mine"))


@bp.post("/my-contributions/quota-request/cancel")
def cancel_quota_request():
    user = auth.current_user()
    pending = quota.pending_request(user["id"])
    try:
        quota.cancel(pending["id"] if pending else 0, user["id"])
    except ContributionError as exc:
        flash_error(exc)
    else:
        auth.flash_t("contributions.quota.cancelled", "success")
    return redirect(url_for("contributions.mine"))


# ── Review ───────────────────────────────────────────────────────────────────


def _reviewer() -> dict[str, Any]:
    user = auth.current_user()
    if not service.is_reviewer(user):
        abort(403)
    return user


@bp.get("/admin/contributions")
def review_list():
    user = _reviewer()
    rows = service.pending_for_reviewer(user)
    per_page: dict[int, int] = {}
    for row in rows:
        per_page[row["page_id"]] = per_page.get(row["page_id"], 0) + 1
    admin = auth.is_admin(user)
    return render_template(
        "contributions/review_list.html", contributions=rows, per_page=per_page,
        quota_requests=quota.all_pending() if admin else [], is_admin_view=admin,
        settings_values=settings.load(), number_settings=NUMBER_SETTINGS,
    )


@bp.get("/admin/contributions/<int:contribution_id>")
def review_detail(contribution_id: int):
    user = _reviewer()
    contribution = service.get(contribution_id)
    if contribution is None:
        abort(404)
    page = pages.get(contribution["page_id"])
    if not service.can_review(page, user):
        abort(403)
    return render_template(
        "contributions/review_detail.html", contribution=contribution, page=page,
        diff=service.diff_lines(page["content"], contribution["content"]),
        page_changed=contribution.get("base_revision") is not None
        and contribution["base_revision"] != page["revision"],
        max_review_reason=service.MAX_REVIEW_REASON,
    )


@bp.post("/admin/contributions/<int:contribution_id>/approve")
def approve(contribution_id: int):
    user = _reviewer()
    try:
        page = service.approve(contribution_id, user, request.form.get("review_reason", ""))
    except ContributionError as exc:
        flash_error(exc)
        return redirect(url_for("contributions.review_detail", contribution_id=contribution_id)
                        if service.get(contribution_id) else url_for("contributions.review_list"))
    auth.flash_t("contributions.approved", "success", title=page["title"])
    return redirect(url_for("contributions.review_list"))


@bp.post("/admin/contributions/<int:contribution_id>/deny")
def deny(contribution_id: int):
    user = _reviewer()
    try:
        service.deny(contribution_id, user, request.form.get("review_reason", ""))
    except ContributionError as exc:
        flash_error(exc)
    else:
        auth.flash_t("contributions.denied", "success")
    return redirect(url_for("contributions.review_list"))


@bp.post("/admin/contributions/<int:contribution_id>/set-quota")
@auth.admin_required
def set_quota(contribution_id: int):
    contribution = service.get(contribution_id)
    if contribution is None:
        abort(404)
    value = (request.form.get("quota") or "").strip()
    try:
        quota.set_quota(contribution["user_id"], None if value in ("", "default") else quota.normalize(value))
    except ContributionError as exc:
        flash_error(exc)
    else:
        auth.flash_t("contributions.quota.updated_for", "success", user=contribution["username"])
    return redirect(url_for("contributions.review_list"))


@bp.post("/admin/contribution-quota-requests/<int:request_id>/review")
@auth.admin_required
def review_quota_request(request_id: int):
    action = request.form.get("action")
    if action not in ("approve", "deny"):
        abort(400)
    try:
        row = quota.review(request_id, auth.current_user()["id"], approve=action == "approve",
                           reason=request.form.get("review_reason", ""))
    except ContributionError as exc:
        flash_error(exc)
    else:
        target = accounts.by_id(row["user_id"])
        auth.flash_t("contributions.quota.reviewed", "success", user=target["username"] if target else "")
    return redirect(url_for("contributions.review_list"))


@bp.post("/admin/contributions/settings")
@auth.admin_required
def save_settings():
    values: dict[str, Any] = {}
    for name, minimum, maximum in NUMBER_SETTINGS:
        number = request.form.get(name, type=int)
        if number is None or not minimum <= number <= maximum:
            auth.flash_t("contributions.error.invalid_number", "error", minimum=minimum, maximum=maximum)
            return redirect(url_for("contributions.review_list"))
        values[name] = number
    settings.update(values)
    auth.flash_t("common.saved", "success")
    return redirect(url_for("contributions.review_list"))
