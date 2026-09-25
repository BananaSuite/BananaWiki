"""Administration: contributions."""

from flask import render_template, request, redirect, url_for, flash, abort, g
import db
from bananawiki_sdk import emit_hook
from helpers import (
    login_required,
    admin_required,
    get_current_user,
    rate_limit,
    t,
)
from wiki_logger import log_action
from sync import notify_change


def register_admin_contributions_routes(app):
    """Register administration routes for contributions."""

    @app.route("/admin/contributions")
    @login_required
    @admin_required
    def admin_contributions():
        """Admin dashboard for managing pending contributions."""
        settings = db.get_site_settings() or {}
        _ep = getattr(g, "enabled_plugins", {}) or {}
        if not _ep.get("page_governance") or not settings.get(
            "contribution_approval_enabled"
        ):
            flash(t("flash.the_contribution_approval_system_is_not_enabled"), "error")
            return redirect(url_for("admin_settings"))
        contributions = db.list_pending_contributions()
        # Group by page for conflict detection
        page_conflicts = {}
        for c in contributions:
            pid = c["page_id"]
            if pid not in page_conflicts:
                page_conflicts[pid] = []
            page_conflicts[pid].append(c)
        conflicts = {
            pid: items for pid, items in page_conflicts.items() if len(items) > 1
        }
        # Also load pending quota requests
        quota_requests = []
        try:
            with db.get_db_context() as conn:
                rows = conn.execute(
                    "SELECT cqr.*, u.username, reviewer.username AS reviewed_by_username "
                    "FROM contribution_quota_requests cqr "
                    "JOIN users u ON u.id = cqr.user_id "
                    "LEFT JOIN users reviewer ON reviewer.id = cqr.reviewed_by "
                    "WHERE cqr.status='pending' "
                    "ORDER BY cqr.created_at ASC"
                ).fetchall()
                quota_requests = [dict(r) for r in rows]
        except Exception:
            pass
        return render_template(
            "admin/contributions.html",
            contributions=contributions,
            conflicts=conflicts,
            quota_requests=quota_requests,
        )

    @app.route("/admin/contributions/<int:contribution_id>/approve", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_approve_contribution(contribution_id):
        """Approve a pending contribution and apply the edit."""
        contribution = db.get_contribution(contribution_id)
        if not contribution:
            abort(404)
        if contribution["status"] != "pending":
            flash(t("flash.this_contribution_is_no_longer_pending"), "error")
            return redirect(url_for("admin_contributions"))
        user = get_current_user()
        review_reason = request.form.get("review_reason", "").strip()

        # Approve the contribution (marks it as approved)
        try:
            approved = db.approve_contribution(
                contribution_id, user["id"], review_reason
            )
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("admin_contributions"))
        if not approved:
            flash(t("flash.failed_to_approve_contribution"), "error")
            return redirect(url_for("admin_contributions"))

        # Apply the edit to the page
        page = db.get_page(approved["page_id"])
        if page:
            db.update_page(
                approved["page_id"],
                approved["title"] or page["title"],
                approved["content"],
                user["id"],
                f"Approved contribution by {contribution.get('username', 'unknown')} (contribution #{contribution_id})",
                builder_json="",
                builder_public=False,
            )
            # Clean up any drafts for this page from the contributor
            db.delete_draft(approved["page_id"], approved["user_id"])
            notify_change(
                "page_edit", f"Page '{page['slug']}' edited via approved contribution"
            )
            emit_hook("after_page_update", page=db.get_page(page["id"]), user=user)

        log_action("approve_contribution", request, user=user, page=contribution_id)
        flash(t("flash.contribution_approved_and_applied"), "success")
        return redirect(url_for("admin_contributions"))

    @app.route("/admin/contributions/<int:contribution_id>/deny", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_deny_contribution(contribution_id):
        """Deny a pending contribution."""
        contribution = db.get_contribution(contribution_id)
        if not contribution:
            abort(404)
        if contribution["status"] != "pending":
            flash(t("flash.this_contribution_is_no_longer_pending"), "error")
            return redirect(url_for("admin_contributions"))
        user = get_current_user()
        review_reason = request.form.get("review_reason", "").strip()
        try:
            db.deny_contribution(contribution_id, user["id"], review_reason)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("admin_contributions"))
        log_action("deny_contribution", request, user=user, page=contribution_id)
        flash(t("flash.contribution_denied"), "success")
        return redirect(url_for("admin_contributions"))

    @app.route("/admin/contributions/<int:contribution_id>/set-quota", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_set_contribution_quota(contribution_id):
        """Set the contribution quota for the user who submitted a contribution."""
        contribution = db.get_contribution(contribution_id)
        if not contribution:
            abort(404)
        user = get_current_user()
        target_username = (
            contribution["username"]
            if "username" in contribution.keys() and contribution["username"]
            else contribution["user_id"]
        )
        quota_str = request.form.get("quota", "").strip()
        try:
            if quota_str in ("", "default", "reset"):
                db.set_user_contribution_quota(contribution["user_id"], None)
                log_action(
                    "admin_set_contribution_quota",
                    request,
                    user=user,
                    target_user=target_username,
                    target_user_id=contribution["user_id"],
                    quota="default",
                )
                flash(t("flash.contribution_quota_reset_to_default"), "success")
            else:
                quota = int(quota_str)
                db.set_user_contribution_quota(contribution["user_id"], quota)
                log_action(
                    "admin_set_contribution_quota",
                    request,
                    user=user,
                    target_user=target_username,
                    target_user_id=contribution["user_id"],
                    quota=quota,
                )
                flash(
                    t("flash.contribution_quota_set_to_quota", quota=quota), "success"
                )
        except (TypeError, ValueError):
            flash(t("flash.invalid_quota_value"), "error")
        return redirect(url_for("admin_contributions"))

    @app.route(
        "/admin/contribution-quota-requests/<int:request_id>/review", methods=["POST"]
    )
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_review_contribution_quota_request(request_id):
        """Approve or deny a contribution quota request."""
        action = request.form.get("action", "").strip()
        if action not in ("approve", "deny"):
            flash(t("flash.invalid_action"), "error")
            return redirect(url_for("admin_contributions"))
        user = get_current_user()
        try:
            reviewed_request = db.review_contribution_quota_request(
                request_id,
                user["id"],
                action == "approve",
                review_reason=request.form.get("review_reason", ""),
            )
            target = db.get_user_by_id(reviewed_request["user_id"])
            log_action(
                "review_contribution_quota_request",
                request,
                user=user,
                target_user=target["username"]
                if target
                else reviewed_request["user_id"],
                target_user_id=reviewed_request["user_id"],
                decision=reviewed_request["status"],
                requested_quota=reviewed_request["requested_quota"],
            )
            flash(t("flash.quota_request_actiond", action=action), "success")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("admin_contributions"))
