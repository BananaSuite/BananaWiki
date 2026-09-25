"""Wiki contributions routes."""

from flask import (
    render_template, request, redirect, url_for, flash, abort,
)
import db
from helpers import (
    login_required, get_current_user,
    rate_limit,
    t,
)
from wiki_logger import log_action

from .wiki_common import (
    _abort_if_page_hidden,
    _page_reservations_enabled,
    _page_protection_enabled,
    _contribution_approval_enabled,
    _user_can_edit_page,
    _MAX_PAGE_CONTENT_LENGTH,
)


def register_wiki_contributions_routes(app):
    """Register contributions endpoints on the application."""

    @app.route("/page/<slug>/propose-edit", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 60)
    def propose_edit(slug):
        """Allow users without direct edit access to propose an edit for admin approval."""
        if not _contribution_approval_enabled():
            flash(t("flash.the_contribution_approval_system_is_not_enabled"), "error")
            return redirect(url_for("view_page", slug=slug))
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        # The form is prefilled with the page body, so it is held to the
        # same rules as viewing the page: deindexed pages, builder pages and
        # category read limits all apply here too.
        _abort_if_page_hidden(page, user)

        # If user can edit directly, redirect them to the normal edit route
        if _user_can_edit_page(user, page):
            return redirect(url_for("edit_page", slug=slug))

        # User must have the propose permission
        if not db.has_permission(user, "contribution.propose"):
            flash(t("flash.you_do_not_have_permission_to_propose_edits"), "error")
            return redirect(url_for("view_page", slug=slug))

        # User must have read access to the page's category
        cat_id = page["category_id"]
        if not db.has_category_read_access(user, cat_id):
            flash(t("flash.you_do_not_have_access_to_this_pages"), "error")
            return redirect(url_for("view_page", slug=slug))

        # Cannot propose edits for pages pending deletion
        if page["pending_deletion"]:
            flash(t("flash.this_page_is_pending_deletion_and_cannot_be"), "error")
            return redirect(url_for("view_page", slug=slug))

        # Check if user already has a pending contribution for this page
        existing = db.get_pending_contribution_for_page(page["id"], user["id"])
        if existing:
            flash(
                t("flash.you_already_have_a_pending_contribution_for_this"),
                "error",
            )
            return redirect(url_for("view_page", slug=slug))

        # Check contribution quota
        if not db.can_user_submit_contribution(user["id"]):
            quota = db.get_effective_contribution_quota(user["id"])
            flash(
                t("flash.you_have_reached_your_pending_contribution_limit_quota", quota=quota),
                "error",
            )
            return redirect(url_for("view_page", slug=slug))

        # Check for conflict: other pending contributions or active drafts
        other_contributions = db.list_contributions_for_page(page["id"])
        pending_others = [c for c in other_contributions if c["status"] == "pending" and c["user_id"] != user["id"]]
        other_drafts = list(db.get_drafts_for_page(page["id"]))

        # Check if page is protected (warn but still allow proposal)
        protection_warning = None
        if _page_protection_enabled() and page["protected_by"]:
            protector = db.get_user_by_id(page["protected_by"])
            protection_warning = (
                f"This page is currently protected by {protector['username'] if protector else 'another user'}. "
                "Your contribution will still be submitted, but may conflict with their changes."
            )

        # Check if page is reserved (warn but still allow proposal)
        reservation_warning = None
        if _page_reservations_enabled():
            reservation = db.get_page_reservation_status(page["id"])
            if reservation and reservation["reserved_by"] and reservation["reserved_by"] != user["id"]:
                reserver = db.get_user_by_id(reservation["reserved_by"])
                reservation_warning = (
                    f"This page is currently reserved by {reserver['username'] if reserver else 'another user'}. "
                    "Your contribution may conflict with their in-progress changes."
                )

        if request.method == "POST":
            title = request.form.get("title", page["title"]).strip()
            content = request.form.get("content", "")
            reason = request.form.get("reason", "").strip()

            if not reason:
                flash(t("flash.you_must_provide_a_reason_for_your_proposed"), "error")
                return render_template(
                    "wiki/propose_edit.html",
                    page=page,
                    other_contributions=pending_others,
                    other_drafts=other_drafts,
                    protection_warning=protection_warning,
                    reservation_warning=reservation_warning,
                )

            if len(content) > _MAX_PAGE_CONTENT_LENGTH:
                flash(t("flash.page_content_is_too_large_maximum_1_mb_0c6658"), "error")
                return render_template(
                    "wiki/propose_edit.html",
                    page=page,
                    other_contributions=pending_others,
                    other_drafts=other_drafts,
                    protection_warning=protection_warning,
                    reservation_warning=reservation_warning,
                )

            if not title:
                title = page["title"]

            # Re-check for existing pending contribution (race condition guard)
            existing = db.get_pending_contribution_for_page(page["id"], user["id"])
            if existing:
                flash(t("flash.you_already_have_a_pending_contribution_for_this"), "error")
                return redirect(url_for("view_page", slug=slug))

            # Re-check permission (user may have been promoted mid-session)
            if _user_can_edit_page(get_current_user(), page):
                flash(t("flash.you_now_have_direct_edit_access_to_this"), "success")
                return redirect(url_for("edit_page", slug=slug))

            try:
                db.create_contribution(page["id"], user["id"], title, content, reason)
            except db.IntegrityError:
                flash(t("flash.you_already_have_a_pending_contribution_for_this"), "error")
                return redirect(url_for("view_page", slug=slug))
            except ValueError as e:
                flash(str(e), "error")
                return render_template(
                    "wiki/propose_edit.html",
                    page=page,
                    other_contributions=pending_others,
                    other_drafts=other_drafts,
                    protection_warning=protection_warning,
                    reservation_warning=reservation_warning,
                )

            log_action("propose_edit", request, user=user, page=slug)
            flash(
                t("flash.your_proposed_edit_has_been_submitted_for_admin"),
                "success",
            )
            return redirect(url_for("view_page", slug=slug))

        return render_template(
            "wiki/propose_edit.html",
            page=page,
            other_contributions=pending_others,
            other_drafts=other_drafts,
            protection_warning=protection_warning,
            reservation_warning=reservation_warning,
        )


    @app.route("/page/<slug>/contribution/<int:contribution_id>/withdraw", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def withdraw_contribution(slug, contribution_id):
        """Allow a user to withdraw their own pending contribution."""
        contribution = db.get_contribution(contribution_id)
        if not contribution:
            abort(404)
        user = get_current_user()
        if contribution["user_id"] != user["id"]:
            abort(403)
        if contribution["status"] != "pending":
            flash(t("flash.this_contribution_is_no_longer_pending_and_cannot"), "error")
            return redirect(url_for("view_page", slug=slug))
        db.withdraw_contribution(contribution_id, user["id"])
        log_action("withdraw_contribution", request, user=user, page=slug)
        flash(t("flash.your_proposed_edit_has_been_withdrawn"), "success")
        return redirect(url_for("view_page", slug=slug))


    @app.route("/my-contributions")
    @login_required
    def my_contributions():
        """List all contributions submitted by the current user."""
        user = get_current_user()
        contributions = db.list_user_contributions(user["id"])
        contribution_enabled = _contribution_approval_enabled()
        quota = db.get_effective_contribution_quota(user["id"]) if contribution_enabled else None
        pending_count = db.get_user_pending_contribution_count(user["id"]) if contribution_enabled else 0
        pending_quota_request = db.get_pending_contribution_quota_request(user["id"]) if contribution_enabled else None
        quota_requests = db.list_contribution_quota_requests(user["id"]) if contribution_enabled else []
        return render_template(
            "wiki/my_contributions.html",
            contributions=contributions,
            contribution_enabled=contribution_enabled,
            quota=quota,
            pending_count=pending_count,
            pending_quota_request=pending_quota_request,
            quota_requests=quota_requests,
            max_reason_length=db.CONTRIBUTION_MAX_QUOTA_REQUEST_REASON_LENGTH,
        )


    @app.route("/my-contributions/quota-request", methods=["POST"])
    @login_required
    @rate_limit(5, 60)
    def contribution_quota_request():
        """Submit a request for a higher contribution quota."""
        if not _contribution_approval_enabled():
            flash(t("flash.the_contribution_approval_system_is_not_enabled"), "error")
            return redirect(url_for("my_contributions"))
        user = get_current_user()
        # Users who can edit all pages don't need a contribution quota
        if db.has_permission(user, "page.edit_all"):
            flash(t("flash.you_have_direct_edit_access_and_do_not"), "error")
            return redirect(url_for("my_contributions"))
        # User must have the propose permission to request a quota
        if not db.has_permission(user, "contribution.propose"):
            flash(t("flash.you_do_not_have_permission_to_propose_edits"), "error")
            return redirect(url_for("my_contributions"))
        requested_quota = request.form.get("requested_quota", "").strip()
        reason = request.form.get("reason", "").strip()
        if request.form.get("unlimited") == "1":
            requested_quota = "-1"
        try:
            requested_quota = int(requested_quota)
        except (TypeError, ValueError):
            flash(t("flash.invalid_quota_value"), "error")
            return redirect(url_for("my_contributions"))
        if requested_quota != -1 and requested_quota < 1:
            flash(t("flash.quota_must_be_at_least_1_or_1"), "error")
            return redirect(url_for("my_contributions"))
        try:
            quota_request = db.create_contribution_quota_request(
                user["id"], requested_quota, reason
            )
            if quota_request["status"] == "approved":
                flash(t("flash.quota_request_automatically_approved"), "success")
            else:
                flash(t("flash.quota_request_submitted_for_admin_review"), "success")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("my_contributions"))


    @app.route("/my-contributions/quota-request/cancel", methods=["POST"])
    @login_required
    @rate_limit(5, 60)
    def cancel_contribution_quota_request_route():
        """Cancel a pending contribution quota request."""
        user = get_current_user()
        pending = db.get_pending_contribution_quota_request(user["id"])
        if not pending:
            flash(t("flash.no_pending_quota_request_to_cancel"), "error")
            return redirect(url_for("my_contributions"))
        try:
            db.cancel_contribution_quota_request(pending["id"], user["id"])
            flash(t("flash.quota_request_cancelled"), "success")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("my_contributions"))


    @app.route("/page/<slug>/contribution/<int:contribution_id>/edit", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 60)
    def edit_contribution(slug, contribution_id):
        """Allow a user to edit their own pending contribution (change and resend)."""
        contribution = db.get_contribution(contribution_id)
        if not contribution:
            abort(404)
        user = get_current_user()
        if contribution["user_id"] != user["id"]:
            abort(403)
        if contribution["status"] != "pending":
            flash(t("flash.only_pending_contributions_can_be_edited"), "error")
            return redirect(url_for("view_page", slug=slug))
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        # A contribution never gives more access than the page itself.
        _abort_if_page_hidden(page, user)

        # If user gained direct edit access, withdraw and redirect
        if _user_can_edit_page(user, page):
            db.withdraw_contribution(contribution_id, user["id"])
            flash(t("flash.you_now_have_direct_edit_access_your_pending"), "info")
            return redirect(url_for("edit_page", slug=slug))

        if request.method == "POST":
            title = request.form.get("title", page["title"]).strip()
            content = request.form.get("content", "")
            reason = request.form.get("reason", "").strip()

            if not reason:
                flash(t("flash.you_must_provide_a_reason_for_your_proposed"), "error")
                return render_template("wiki/edit_contribution.html", page=page, contribution=contribution)

            if len(content) > _MAX_PAGE_CONTENT_LENGTH:
                flash(t("flash.page_content_is_too_large_maximum_1_mb_0c6658"), "error")
                return render_template("wiki/edit_contribution.html", page=page, contribution=contribution)

            if not title:
                title = page["title"]

            try:
                db.update_contribution(contribution_id, title, content, reason)
            except ValueError as e:
                flash(str(e), "error")
                return render_template("wiki/edit_contribution.html", page=page, contribution=contribution)
            flash(t("flash.contribution_updated"), "success")
            return redirect(url_for("view_page", slug=slug))
        return render_template("wiki/edit_contribution.html", page=page, contribution=contribution)
