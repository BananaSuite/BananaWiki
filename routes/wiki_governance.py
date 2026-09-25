"""Wiki governance routes."""

from flask import (
    render_template, request, redirect, url_for, flash, abort,
)
import db
from helpers import (
    login_required, editor_required, get_current_user,
    editor_has_category_access, rate_limit,
    _safe_referrer, get_request_category_tree,
    t,
)
from wiki_logger import log_action
from sync import notify_change

from .wiki_common import (
    _page_reservations_enabled,
    _page_protection_enabled,
    _iter_reservable_pages,
)


def register_wiki_governance_routes(app):
    """Register governance endpoints on the application."""

    @app.route("/reservations")
    @login_required
    @editor_required
    def reservation_directory():
        """List reservable pages and their current reservation state."""
        if not _page_reservations_enabled():
            flash(t("flash.page_reservations_are_currently_disabled"), "error")
            return redirect(url_for("home"))
        user = get_current_user()
        try:
            categories, uncategorized = get_request_category_tree()
        except Exception:
            categories, uncategorized = [], []
        reservable_pages = _iter_reservable_pages(categories, uncategorized, user)
        return render_template(
            "wiki/reservations.html",
            reservable_pages=reservable_pages,
        )


    @app.route("/page/<slug>/reserve", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(30, 60)
    def reserve_page_route(slug):
        """Reserve a page from the standard web UI."""
        if not _page_reservations_enabled():
            flash(t("flash.page_reservations_are_currently_disabled"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if page["is_home"]:
            flash(t("flash.the_home_page_cannot_be_reserved"), "error")
            return redirect(_safe_referrer() or url_for("view_page", slug=slug))
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_edit_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        try:
            db.reserve_page(page["id"], user["id"])
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            log_action("reserve_page", request, user=user, page=slug)
            notify_change("page_reservation", f"Page '{page['title']}' reserved by {user['username']}")
            flash(t("flash.page_has_been_successfully_reserved_for_your_editing"), "success")
        return redirect(_safe_referrer() or url_for("view_page", slug=slug))


    @app.route("/page/<slug>/reservation/release", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(30, 60)
    def release_page_reservation_route(slug):
        """Release a page reservation from the standard web UI."""
        if not _page_reservations_enabled():
            flash(t("flash.page_reservations_are_currently_disabled"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_edit_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        if db.release_page_reservation(page["id"], user["id"]):
            log_action("release_page_reservation", request, user=user, page=slug)
            notify_change("page_reservation", f"Page '{page['title']}' reservation released by {user['username']}")
            flash(t("flash.page_reservation_has_been_successfully_released"), "success")
        else:
            flash(t("flash.you_do_not_currently_hold_an_active_reservation"), "error")
        return redirect(_safe_referrer() or url_for("view_page", slug=slug))


    @app.route("/page/<slug>/protection", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def update_page_protection(slug):
        """Protect or unprotect a page for exclusive editing ownership."""
        if not _page_protection_enabled():
            flash(t("flash.page_protection_is_currently_disabled"), "error")
            return redirect(url_for("home"))
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if page["is_home"]:
            flash(t("flash.the_home_page_cannot_be_protected"), "error")
            return redirect(url_for("view_page", slug=slug))
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_edit_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        action = (request.form.get("action") or "").strip().lower()
        if action == "protect":
            if page["protected_by"]:
                owner = db.get_user_by_id(page["protected_by"])
                owner_name = owner["username"] if owner else "[deleted user]"
                if page["protected_by"] == user["id"]:
                    flash(t("flash.this_page_is_already_protected_by_you"), "info")
                else:
                    flash(t("flash.this_page_is_already_protected_by_ownername", owner_name=owner_name), "error")
                return redirect(url_for("view_page", slug=slug))
            db.set_page_protection(page["id"], user["id"])
            log_action("protect_page", request, user=user, page=slug)
            notify_change("page_protect", f"Page '{slug}' protected")
            flash(t("flash.page_protection_has_been_successfully_enabled_for_you"), "success")
            return redirect(url_for("view_page", slug=slug))
        if action == "unprotect":
            if not page["protected_by"]:
                flash(t("flash.this_page_is_not_currently_protected"), "info")
                return redirect(url_for("view_page", slug=slug))
            if page["protected_by"] != user["id"]:
                flash(
                    t("flash.only_the_user_who_protected_this_page_can"),
                    "error",
                )
                return redirect(url_for("view_page", slug=slug))
            db.clear_page_protection(page["id"])
            log_action("unprotect_page", request, user=user, page=slug)
            notify_change("page_unprotect", f"Page '{slug}' protection removed")
            flash(t("flash.page_protection_has_been_successfully_removed"), "success")
            return redirect(url_for("view_page", slug=slug))
        flash(t("flash.invalid_page_protection_action"), "error")
        return redirect(url_for("view_page", slug=slug))
