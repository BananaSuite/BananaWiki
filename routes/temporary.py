"""
BananaWiki: Temporary accounts & pages management routes.

Admin routes for setting auto-deletion schedules on pages, user accounts,
and temporary role grants.
"""

from datetime import datetime, timezone

from flask import (
    render_template, request, redirect, url_for, flash,
)

import db
from helpers import (
    login_required, admin_required, get_current_user, rate_limit,
    local_datetime_to_utc,
    t,
)
from wiki_logger import log_action
from sync import notify_change


def _is_expiry_in_past(expires_at_utc_str):
    """Return True if the UTC ISO datetime string is not in the future."""
    return (
        datetime.fromisoformat(expires_at_utc_str).replace(tzinfo=timezone.utc)
        <= datetime.now(timezone.utc)
    )


def register_temporary_routes(app):
    """Register temporary accounts & pages routes on *app*."""

    @app.route("/admin/temporary")
    @login_required
    @admin_required
    def admin_temporary():
        """Admin page for managing temporary pages, users, and roles."""
        # Run cleanup proactively so the admin sees accurate data
        db.cleanup_all_expired_temporary()
        temp_pages = db.list_temp_pages()
        temp_users = db.list_temp_users()
        temp_roles = db.list_temp_roles()
        temp_page_index_states = db.list_temp_page_index_states()
        users = db.list_users()
        # Get all pages for the page selector
        all_pages = db.search_pages("")
        return render_template(
            "admin/temporary.html",
            temp_pages=temp_pages,
            temp_users=temp_users,
            temp_roles=temp_roles,
            temp_page_index_states=temp_page_index_states,
            users=users,
            all_pages=all_pages,
        )

    @app.route("/admin/temporary/page", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_set_page_expiry():
        """Set or update auto-deletion schedule for a page."""
        page_id = request.form.get("page_id", "").strip()
        expires_at = request.form.get("expires_at", "").strip() or None
        show_countdown = request.form.get("show_countdown") == "1"
        admin = get_current_user()

        if not page_id:
            flash(t("flash.page_is_required_to_continue"), "error")
            return redirect(url_for("admin_temporary"))

        try:
            page_id = int(page_id)
        except (ValueError, TypeError):
            flash(t("flash.invalid_page_id"), "error")
            return redirect(url_for("admin_temporary"))

        page = db.get_page(page_id)
        if not page:
            flash(t("flash.page_not_found"), "error")
            return redirect(url_for("admin_temporary"))

        # Warn about reserved pages
        reservation = db.get_page_reservation_status(page_id)
        if reservation and reservation.get("is_reserved"):
            flash(
                t("flash.warning_this_page_is_currently_reserved_by_user", user=reservation.get('reserved_by_username', 'another user')),
                "warning",
            )

        if page["is_home"]:
            if expires_at:
                flash(t("flash.cannot_schedule_home_page_for_autodeletion"), "error")
                return redirect(url_for("admin_temporary"))
            flash(t("flash.cannot_schedule_home_page_for_autodeletion"), "info")

        if expires_at:
            try:
                expires_at = local_datetime_to_utc(expires_at)
            except ValueError:
                flash(t("flash.invalid_expiration_date_format"), "error")
                return redirect(url_for("admin_temporary"))

            if _is_expiry_in_past(expires_at):
                flash(t("flash.expiry_date_must_be_in_the_future"), "error")
                return redirect(url_for("admin_temporary"))

        db.set_page_expiry(
            page_id, expires_at, show_countdown=show_countdown,
            set_by=admin["id"],
        )

        if expires_at:
            log_action("set_page_expiry", request, user=admin,
                       page_title=page["title"], expires_at=expires_at)
            notify_change("page_expiry_set",
                          f"Page '{page['title']}' scheduled for deletion at {expires_at}")
            flash(t("flash.page_title_scheduled_for_autodeletion", title=page['title']), "success")
        else:
            log_action("clear_page_expiry", request, user=admin,
                       page_title=page["title"])
            flash(t("flash.autodeletion_removed_from_title", title=page['title']), "success")

        return redirect(url_for("admin_temporary"))

    @app.route("/admin/temporary/page/<int:page_id>/remove", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_remove_page_expiry(page_id):
        """Remove the auto-deletion schedule from a page (make it permanent)."""
        admin = get_current_user()
        page = db.get_page(page_id)
        if not page:
            flash(t("flash.page_not_found"), "error")
            return redirect(url_for("admin_temporary"))

        db.set_page_expiry(page_id, None)
        log_action("clear_page_expiry", request, user=admin, page_title=page["title"])
        flash(t("flash.autodeletion_removed_from_title", title=page['title']), "success")
        return redirect(url_for("admin_temporary"))

    @app.route("/admin/temporary/user", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_set_user_expiry():
        """Set or update auto-deletion schedule for a user account."""
        user_id = request.form.get("user_id", "").strip()
        expires_at = request.form.get("expires_at", "").strip() or None
        show_countdown = request.form.get("show_countdown") == "1"
        admin = get_current_user()

        if not user_id:
            flash(t("flash.user_is_required_to_continue"), "error")
            return redirect(url_for("admin_temporary"))

        target = db.get_user_by_id(user_id)
        if not target:
            flash(t("flash.user_not_found"), "error")
            return redirect(url_for("admin_temporary"))

        if target["role"] == "owner":
            flash(t("flash.owner_accounts_cannot_be_set_for_autodeletion"), "error")
            return redirect(url_for("admin_temporary"))

        if user_id == admin["id"]:
            flash(t("flash.you_cannot_set_autodeletion_on_your_own_account"), "error")
            return redirect(url_for("admin_temporary"))

        if "is_superuser" in target.keys() and target["is_superuser"]:
            flash(t("flash.this_account_is_protected_and_cannot_be_modified_0e7d81"), "error")
            return redirect(url_for("admin_temporary"))

        if expires_at:
            try:
                expires_at = local_datetime_to_utc(expires_at)
            except ValueError:
                flash(t("flash.invalid_expiration_date_format"), "error")
                return redirect(url_for("admin_temporary"))

            if _is_expiry_in_past(expires_at):
                flash(t("flash.expiry_date_must_be_in_the_future"), "error")
                return redirect(url_for("admin_temporary"))

        db.set_user_expiry(
            user_id, expires_at, show_countdown=show_countdown,
            set_by=admin["id"],
        )

        if expires_at:
            log_action("set_user_expiry", request, user=admin,
                       target_user=target["username"], expires_at=expires_at)
            notify_change("user_expiry_set",
                          f"User '{target['username']}' scheduled for deletion at {expires_at}")
            flash(t("flash.user_username_scheduled_for_autodeletion", username=target['username']), "success")
        else:
            log_action("clear_user_expiry", request, user=admin,
                       target_user=target["username"])
            flash(t("flash.autodeletion_removed_from_username", username=target['username']), "success")

        return redirect(url_for("admin_temporary"))

    @app.route("/admin/temporary/user/<string:user_id>/remove", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_remove_user_expiry(user_id):
        """Remove the auto-deletion schedule from a user (make permanent)."""
        admin = get_current_user()
        target = db.get_user_by_id(user_id)
        if not target:
            flash(t("flash.user_not_found"), "error")
            return redirect(url_for("admin_temporary"))

        if target["role"] == "owner":
            flash(t("flash.owner_accounts_cannot_be_set_for_autodeletion"), "error")
            return redirect(url_for("admin_temporary"))

        if user_id == admin["id"]:
            flash(t("flash.you_cannot_set_autodeletion_on_your_own_account"), "error")
            return redirect(url_for("admin_temporary"))

        if "is_superuser" in target.keys() and target["is_superuser"]:
            flash(t("flash.this_account_is_protected_and_cannot_be_modified_0e7d81"), "error")
            return redirect(url_for("admin_temporary"))

        db.set_user_expiry(user_id, None)
        log_action("clear_user_expiry", request, user=admin, target_user=target["username"])
        flash(t("flash.autodeletion_removed_from_username", username=target['username']), "success")
        return redirect(url_for("admin_temporary"))

    @app.route("/admin/temporary/role", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_set_role_expiry():
        """Set or update a temporary role grant with auto-revocation."""
        user_id = request.form.get("user_id", "").strip()
        original_role = request.form.get("original_role", "").strip()
        expires_at = request.form.get("expires_at", "").strip() or None
        show_countdown = request.form.get("show_countdown") == "1"
        admin = get_current_user()

        if not user_id:
            flash(t("flash.user_is_required_to_continue"), "error")
            return redirect(url_for("admin_temporary"))

        target = db.get_user_by_id(user_id)
        if not target:
            flash(t("flash.user_not_found"), "error")
            return redirect(url_for("admin_temporary"))

        if target["role"] == "owner":
            flash(t("flash.owner_accounts_cannot_have_temporary_role_grants"), "error")
            return redirect(url_for("admin_temporary"))

        if user_id == admin["id"]:
            flash(t("flash.you_cannot_set_a_temporary_role_on_your"), "error")
            return redirect(url_for("admin_temporary"))

        # The user list refuses every change to a superuser account, and a
        # schedule would demote one at expiry, so none can be set here either.
        # Removing an existing schedule stays possible: it only protects.
        if "is_superuser" in target.keys() and target["is_superuser"]:
            flash(t("flash.this_account_is_protected_and_cannot_be_modified_0e7d81"), "error")
            return redirect(url_for("admin_temporary"))

        if not original_role or original_role not in ("user", "editor", "admin"):
            flash(t("flash.invalid_original_role"), "error")
            return redirect(url_for("admin_temporary"))

        # The original role must be lower than the current role for it to make sense
        role_order = {"user": 0, "editor": 1, "admin": 2, "owner": 3}
        if role_order.get(original_role, 0) >= role_order.get(target["role"], 0):
            flash(
                t("flash.the_revert_role_originalrole_must_be_lower_than", original_role=original_role, role=target['role']),
                "error",
            )
            return redirect(url_for("admin_temporary"))

        if expires_at:
            try:
                expires_at = local_datetime_to_utc(expires_at)
            except ValueError:
                flash(t("flash.invalid_expiration_date_format"), "error")
                return redirect(url_for("admin_temporary"))

            if _is_expiry_in_past(expires_at):
                flash(t("flash.expiry_date_must_be_in_the_future"), "error")
                return redirect(url_for("admin_temporary"))

        db.set_role_expiry(
            user_id, original_role, expires_at=expires_at,
            show_countdown=show_countdown, set_by=admin["id"],
        )

        if expires_at:
            log_action("set_role_expiry", request, user=admin,
                       target_user=target["username"],
                       original_role=original_role, expires_at=expires_at)
            notify_change(
                "role_expiry_set",
                f"User '{target['username']}' role will revert to "
                f"'{original_role}' at {expires_at}",
            )
            flash(
                t("flash.temporary_role_set_for_username_role_will_revert", username=target['username'], original_role=original_role),
                "success",
            )
        else:
            log_action("clear_role_expiry", request, user=admin,
                       target_user=target["username"])
            flash(t("flash.temporary_role_schedule_removed_from_username", username=target['username']), "success")

        return redirect(url_for("admin_temporary"))

    @app.route("/admin/temporary/role/<string:user_id>/remove", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_remove_role_expiry(user_id):
        """Remove the auto-revocation schedule from a user's role."""
        admin = get_current_user()
        target = db.get_user_by_id(user_id)
        if not target:
            flash(t("flash.user_not_found"), "error")
            return redirect(url_for("admin_temporary"))

        if target["role"] == "owner":
            flash(t("flash.owner_accounts_cannot_have_temporary_role_grants"), "error")
            return redirect(url_for("admin_temporary"))

        if user_id == admin["id"]:
            flash(t("flash.you_cannot_set_a_temporary_role_on_your"), "error")
            return redirect(url_for("admin_temporary"))

        db.set_role_expiry(user_id, "user", expires_at=None)
        log_action("clear_role_expiry", request, user=admin, target_user=target["username"])
        flash(t("flash.temporary_role_schedule_removed_from_username", username=target['username']), "success")
        return redirect(url_for("admin_temporary"))
