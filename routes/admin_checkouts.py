"""Administration: checkouts."""

from flask import render_template, request, redirect, url_for, flash, abort
import db
from helpers import (
    login_required,
    admin_required,
    get_current_user,
    rate_limit,
    get_request_category_tree,
    t,
)
from wiki_logger import log_action
from sync import notify_change


def register_admin_checkouts_routes(app):
    """Register administration routes for checkouts."""

    @app.route("/admin/checkouts")
    @login_required
    @admin_required
    def admin_checkouts():
        """Admin: view all active page reservations and cooldowns."""
        reservations = db.get_all_active_reservations()
        cooldowns = db.get_all_active_cooldowns()
        # All pages (including deindexed) for the assign dropdown
        all_users = db.list_users()
        eligible_users = [
            u for u in all_users if u["role"] in ("editor", "admin", "owner", "user")
        ]
        # Collect all pages for the page picker
        try:
            cat_tree, uncat = get_request_category_tree()
        except Exception:
            cat_tree, uncat = [], []
        all_pages = []

        def _collect_pages(cats):
            """Recursively collect pages from category tree into all_pages."""
            for cat in cats:
                all_pages.extend(cat["pages"])
                _collect_pages(cat["children"])

        _collect_pages(cat_tree)
        all_pages.extend(uncat)
        # Also grab deindexed pages not covered by tree (tree hides none, but be safe)
        return render_template(
            "admin/checkouts.html",
            reservations=reservations,
            cooldowns=cooldowns,
            eligible_users=eligible_users,
            all_pages=sorted(all_pages, key=lambda p: p["title"].lower()),
        )

    @app.route("/admin/checkouts/<int:page_id>/release", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_force_release_reservation(page_id):
        """Admin: force-release a page reservation, bypassing normal ownership checks."""
        user = get_current_user()
        page = db.get_page(page_id)
        if not page:
            abort(404)
        released = db.force_release_reservation(page_id)
        if released:
            log_action(
                "admin_force_release_checkout", request, user=user, page=page["slug"]
            )
            notify_change(
                "reservation_release",
                f"Reservation on '{page['title']}' force-released by admin",
            )
            flash(
                t(
                    "flash.reservation_on_title_has_been_successfully_released",
                    title=page["title"],
                ),
                "success",
            )
        else:
            flash(t("flash.no_active_reservation_found_for_that_page"), "info")
        return redirect(url_for("admin_checkouts"))

    @app.route("/admin/checkouts/<int:page_id>/assign", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_assign_reservation(page_id):
        """Admin: assign a page reservation to a specific user, bypassing all checks."""
        user = get_current_user()
        page = db.get_page(page_id)
        if not page:
            abort(404)
        target_user_id = request.form.get("user_id", "").strip()
        if not target_user_id:
            flash(t("flash.a_user_must_be_selected_to_assign_the"), "error")
            return redirect(url_for("admin_checkouts"))
        target_user = db.get_user_by_id(target_user_id)
        if not target_user:
            flash(t("flash.the_selected_user_does_not_exist"), "error")
            return redirect(url_for("admin_checkouts"))
        try:
            db.admin_assign_reservation(page_id, target_user_id)
            log_action(
                "admin_assign_reservation",
                request,
                user=user,
                page=page["slug"],
                target_user=target_user["username"],
            )
            notify_change(
                "reservation_assign",
                f"Reservation on '{page['title']}' assigned to {target_user['username']} by admin",
            )
            flash(
                t(
                    "flash.reservation_on_title_has_been_successfully_assigned_to",
                    title=page["title"],
                    username=target_user["username"],
                ),
                "success",
            )
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("admin_checkouts"))

    @app.route("/admin/checkouts/assign-new", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_assign_new_reservation():
        """Admin: assign a new reservation by selecting page from list or entering a slug."""
        user = get_current_user()
        page_id_raw = request.form.get("page_id", "").strip()
        page_slug = request.form.get("page_slug", "").strip()
        # Resolve page: by slug first, then by numeric ID
        page = None
        if page_slug:
            page = db.get_page_by_slug(page_slug)
        if not page and page_id_raw:
            try:
                page = db.get_page(int(page_id_raw))
            except (ValueError, TypeError):
                pass
        if not page:
            flash(t("flash.page_not_found_please_select_a_valid_page"), "error")
            return redirect(url_for("admin_checkouts"))
        target_user_id = request.form.get("user_id", "").strip()
        if not target_user_id:
            flash(t("flash.a_user_must_be_selected_to_assign_the"), "error")
            return redirect(url_for("admin_checkouts"))
        target_user = db.get_user_by_id(target_user_id)
        if not target_user:
            flash(t("flash.the_selected_user_does_not_exist"), "error")
            return redirect(url_for("admin_checkouts"))
        try:
            db.admin_assign_reservation(page["id"], target_user_id)
            log_action(
                "admin_assign_reservation",
                request,
                user=user,
                page=page["slug"],
                target_user=target_user["username"],
            )
            notify_change(
                "reservation_assign",
                f"Reservation on '{page['title']}' assigned to {target_user['username']} by admin",
            )
            flash(
                t(
                    "flash.reservation_on_title_has_been_successfully_assigned_to",
                    title=page["title"],
                    username=target_user["username"],
                ),
                "success",
            )
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("admin_checkouts"))

    @app.route("/admin/checkouts/<int:page_id>/clear-cooldown", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_clear_page_cooldown(page_id):
        """Admin: clear cooldowns for a page, optionally for a specific user."""
        user = get_current_user()
        page = db.get_page(page_id)
        if not page:
            abort(404)
        target_user_id = request.form.get("user_id", "").strip() or None
        count = db.admin_clear_cooldown(page_id, target_user_id)
        if count > 0:
            log_action("admin_clear_cooldown", request, user=user, page=page["slug"])
            flash(
                t(
                    "flash.cleared_count_cooldowns_for_title",
                    count=count,
                    title=page["title"],
                ),
                "success",
            )
        else:
            flash(t("flash.no_active_cooldowns_found_for_that_page"), "info")
        return redirect(url_for("admin_checkouts"))
