"""Account merge pages: requests by account owners and administrator review."""

from __future__ import annotations

from flask import redirect, render_template, request, url_for

from ... import accounts, auth
from ..admin.service import protection_error
from . import merge
from .routes import bp


def _fail(error: merge.MergeError, endpoint: str):
    auth.flash_t(error.key, "error", **error.values)
    return redirect(url_for(endpoint))


# ── Account owners ───────────────────────────────────────────────────────────


@bp.route("/settings/merge-request", methods=["GET", "POST"])
@bp.route("/settings/merge-request/from/<username>", methods=["GET", "POST"])
def merge_request(username: str | None = None):
    user = auth.current_user()
    other_name = request.form.get("other_username", username or "").strip()
    if request.method == "POST":
        other = accounts.by_username(other_name)
        if other is None:
            auth.flash_t("users.merge.error.no_such_user", "error")
            return render_template("users/merge_request.html", user=user, tab="account", other_name=other_name)
        try:
            merge.create(user, other, into_mine=request.form.get("direction", "into_mine") != "into_other",
                         reason=request.form.get("reason", ""), password=request.form.get("password"))
        except merge.MergeError as error:
            auth.flash_t(error.key, "error", **error.values)
            return render_template("users/merge_request.html", user=user, tab="account", other_name=other_name)
        auth.flash_t("users.merge.flash.requested", "success")
        return redirect(url_for("users.merge_pending"))
    return render_template("users/merge_request.html", user=user, tab="account", other_name=other_name)


@bp.get("/settings/merge/pending")
def merge_pending():
    user = auth.current_user()
    active, finished = merge.for_user(user["id"])
    return render_template("users/merge_pending.html", user=user, tab="account", active=active, finished=finished)


@bp.post("/settings/merge/approve/<int:merge_id>")
def merge_confirm(merge_id: int):
    try:
        merge.confirm(merge_id, auth.current_user(), request.form.get("password"))
    except merge.MergeError as error:
        return _fail(error, "users.merge_pending")
    auth.flash_t("users.merge.flash.confirmed", "success")
    return redirect(url_for("users.merge_pending"))


def _close_own(merge_id: int, status: str):
    try:
        merge.close(merge_id, status, user=auth.current_user())
    except merge.MergeError as error:
        return _fail(error, "users.merge_pending")
    auth.flash_t(f"users.merge.flash.{status}", "success")
    return redirect(url_for("users.merge_pending"))


@bp.post("/settings/merge/cancel/<int:merge_id>")
def merge_cancel(merge_id: int):
    return _close_own(merge_id, "cancelled")


@bp.post("/settings/merge/deny/<int:merge_id>")
def merge_deny(merge_id: int):
    return _close_own(merge_id, "denied")


# ── Administrators ───────────────────────────────────────────────────────────


@bp.get("/admin/merge-requests")
@auth.admin_required
def admin_merge_requests():
    status = "all" if request.args.get("status") == "all" else "pending"
    return render_template("users/admin_merge_requests.html", requests=merge.listing(status), status=status)


@bp.post("/admin/merge-requests/<int:merge_id>/approve")
@auth.admin_required
def admin_merge_approve(merge_id: int):
    try:
        merge.approve(merge_id, auth.current_user(), delete_source=request.form.get("delete_source") == "1")
    except merge.MergeError as error:
        return _fail(error, "users.admin_merge_requests")
    auth.flash_t("users.merge.flash.merged", "success")
    return redirect(url_for("users.admin_merge_requests"))


def _close_any(merge_id: int, status: str):
    try:
        merge.close(merge_id, status)
    except merge.MergeError as error:
        return _fail(error, "users.admin_merge_requests")
    auth.flash_t(f"users.merge.flash.{status}", "success")
    return redirect(url_for("users.admin_merge_requests"))


@bp.post("/admin/requests/<int:merge_id>/deny")
@auth.admin_required
def admin_merge_deny(merge_id: int):
    return _close_any(merge_id, "denied")


@bp.post("/admin/requests/<int:merge_id>/cancel")
@auth.admin_required
def admin_merge_cancel(merge_id: int):
    return _close_any(merge_id, "cancelled")


@bp.route("/admin/users/merge", methods=["GET", "POST"])
@auth.admin_required
def admin_merge():
    form = request.form
    source_name = form.get("source_username", request.args.get("source", "")).strip()
    target_name = form.get("target_username", request.args.get("target", "")).strip()
    context = {"source_name": source_name, "target_name": target_name,
               "delete_source": form.get("delete_source") == "1", "preview": None}
    if request.method == "GET":
        return render_template("users/admin_merge.html", **context)
    source, target = accounts.by_username(source_name), accounts.by_username(target_name)
    if source is None or target is None:
        auth.flash_t("users.merge.error.no_such_user", "error")
        return render_template("users/admin_merge.html", **context)
    if form.get("action") == "execute":
        # Nobody consented to a direct merge: the administrator must be allowed to change both accounts.
        refusal = protection_error(auth.current_user(), source) or protection_error(auth.current_user(), target)
        if refusal:
            auth.flash_t(refusal, "error")
            return render_template("users/admin_merge.html", **context), 403
        try:
            merge.execute(source, target, auth.current_user(), delete_source=context["delete_source"])
        except merge.MergeError as error:
            auth.flash_t(error.key, "error", **error.values)
            return render_template("users/admin_merge.html", **context)
        auth.flash_t("users.merge.flash.merged", "success")
        return redirect(url_for("users.admin_merge_requests"))
    if source["id"] == target["id"]:
        auth.flash_t("users.merge.error.same_account", "error")
        return render_template("users/admin_merge.html", **context)
    context["preview"] = merge.preview(source)
    return render_template("users/admin_merge.html", **context)
