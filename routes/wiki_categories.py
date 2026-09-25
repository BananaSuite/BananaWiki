"""Wiki categories routes."""

from flask import (
    request, redirect, url_for, flash, abort,
)
import db
from bananawiki_sdk import emit_hook
from helpers import (
    login_required, editor_required, get_current_user,
    editor_has_category_access, rate_limit,
    _safe_referrer, t,
)
from wiki_logger import log_action
from sync import notify_change
from routes.uploads import cleanup_unused_uploads

from .wiki_common import (
    _get_page_protection_context,
    _get_reservation_context,
    _guard_category_management_permission,
)


def _page_delete_blocker(page, user):
    """Return why *user* may not delete *page* right now, or None.

    These are the per-page checks of the single page delete route
    (routes/wiki_editing.py), without its redirects, so that deleting a
    whole category cannot remove a page that could not be deleted on its own.
    """
    protection = _get_page_protection_context(page, user)
    if protection and protection["edit_locked"]:
        return "protected"
    reservation = _get_reservation_context(page, user)
    if reservation and reservation["edit_locked"]:
        return "reserved"
    if db.get_page_expiry(page["id"]) is not None:
        return "scheduled"
    return None


def _delete_category_pages(cat_id, user):
    """Delete or queue every non-home page of category *cat_id*.

    Returns ``(deleted, queued)``.  Pages go through the deletion_slowdown
    grace period exactly as they would from the single page delete route;
    pages that are already queued stay queued.  The caller has already
    checked every page with :func:`_page_delete_blocker`.
    """
    slowdown = db.is_plugin_enabled("deletion_slowdown") and not (
        db.is_docs_category(cat_id)
        and (db.get_site_settings() or {}).get("docs_bypass_deletion_slowdown")
    )
    deleted = queued = 0
    for page in db.get_pages_in_category(cat_id):
        if slowdown:
            if page["pending_deletion"]:
                continue
            if db.mark_page_pending_deletion(page["id"], user["id"]):
                log_action("page_pending_deletion", request, user=user, page=page["slug"])
                notify_change(
                    "page_pending_delete",
                    f"Page '{page['slug']}' queued for deletion (48h grace period)",
                )
                emit_hook("after_page_delete", page=page, user=user)
                queued += 1
            continue
        db.delete_page(page["id"])
        log_action("delete_page", request, user=user, page=page["slug"])
        notify_change("page_delete", f"Page '{page['slug']}' deleted")
        emit_hook("after_page_delete", page=page, user=user)
        deleted += 1
    if deleted:
        cleanup_unused_uploads()
    return deleted, queued


def register_wiki_categories_routes(app):
    """Register categories endpoints on the application."""

    @app.route("/category/create", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def create_category():
        """Create a new category, optionally nested under a parent category."""
        user = get_current_user()
        blocked_response = _guard_category_management_permission(
            user,
            "category.create",
            t("flash.category_create_no_permission"),
        )
        if blocked_response:
            return blocked_response
        name = request.form.get("name", "").strip()
        parent_id = request.form.get("parent_id")
        try:
            parent_id = int(parent_id) if parent_id else None
        except (TypeError, ValueError):
            flash(t("flash.invalid_parent_category"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        if not name:
            flash(t("flash.category_name_is_required"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        if len(name) > 100:
            flash(t("flash.category_name_cannot_exceed_100_characters"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        if parent_id and not db.get_category(parent_id):
            flash(t("flash.selected_parent_category_does_not_exist"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        db.create_category(name, parent_id)
        log_action("create_category", request, user=user, category=name)
        notify_change("category_create", f"Category '{name}' created")
        flash(t("flash.category_has_been_successfully_created"), "success")
        return redirect(_safe_referrer() or url_for("home"))


    @app.route("/category/<int:cat_id>/edit", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def edit_category(cat_id):
        """Rename an existing category."""
        cat = db.get_category(cat_id)
        if not cat:
            abort(404)
        user = get_current_user()
        blocked_response = _guard_category_management_permission(
            user,
            "category.edit",
            t("flash.category_edit_no_permission"),
        )
        if blocked_response:
            return blocked_response
        name = request.form.get("name", "").strip()
        if not name:
            flash(t("flash.category_name_is_required"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        if len(name) > 100:
            flash(t("flash.category_name_cannot_exceed_100_characters"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        db.update_category(cat_id, name)
        log_action("edit_category", request, user=user, category_id=cat_id, new_name=name)
        notify_change("category_edit", f"Category {cat_id} renamed to '{name}'")
        flash(t("flash.category_has_been_successfully_updated"), "success")
        return redirect(_safe_referrer() or url_for("home"))


    @app.route("/category/<int:cat_id>/move", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def move_category(cat_id):
        """Move a category under a different parent in the hierarchy."""
        cat = db.get_category(cat_id)
        if not cat:
            abort(404)
        user = get_current_user()
        blocked_response = _guard_category_management_permission(
            user,
            "category.reorder",
            t("flash.category_move_no_permission"),
        )
        if blocked_response:
            return blocked_response
        parent_id = request.form.get("parent_id")
        try:
            parent_id = int(parent_id) if parent_id else None
        except (TypeError, ValueError):
            parent_id = None
        # Prevent moving a category into itself or a descendant (circular ref)
        if parent_id == cat_id:
            flash(t("flash.cannot_move_a_category_into_itself"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        if parent_id and not db.get_category(parent_id):
            flash(t("flash.the_target_category_no_longer_exists"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        if parent_id and db.is_descendant_of(cat_id, parent_id):
            flash(t("flash.cannot_move_a_category_into_one_of_its"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        db.update_category_parent(cat_id, parent_id)
        log_action("move_category", request, user=user, category_id=cat_id, new_parent=parent_id)
        notify_change("category_move", f"Category {cat_id} moved to parent {parent_id}")
        flash(t("flash.category_has_been_successfully_moved"), "success")
        return redirect(_safe_referrer() or url_for("home"))


    @app.route("/category/<int:cat_id>/delete", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(10, 60)
    def delete_category_route(cat_id):
        """Delete a category and handle its pages according to the chosen action."""
        cat = db.get_category(cat_id)
        if not cat:
            abort(404)
        user = get_current_user()
        blocked_response = _guard_category_management_permission(
            user,
            "category.delete",
            t("flash.category_delete_no_permission"),
        )
        if blocked_response:
            return blocked_response
        # Whatever happens to its pages, deleting a category changes every
        # page in it, so it needs the same category write access as editing
        # those pages.  category.delete alone is granted to editors by
        # default, including editors restricted to other categories.
        if not editor_has_category_access(user, cat_id):
            flash(
                t("flash.category_delete_needs_write_access",
                  default="You can only delete categories whose pages you are allowed to edit."),
                "error",
            )
            return redirect(_safe_referrer() or url_for("home"))
        page_action = request.form.get("page_action", "uncategorize")
        target_cat = request.form.get("target_category_id")
        try:
            target_cat = int(target_cat) if target_cat else None
        except (TypeError, ValueError):
            target_cat = None
        if page_action not in ("uncategorize", "delete", "move"):
            page_action = "uncategorize"
        if page_action == "move" and (not target_cat or target_cat == cat_id
                                      or not db.get_category(target_cat)):
            page_action = "uncategorize"
        if page_action == "move" and not editor_has_category_access(user, target_cat):
            flash(t("flash.you_do_not_have_permission_to_move_pages_4f862e"), "error")
            return redirect(_safe_referrer() or url_for("home"))
        pages = db.get_pages_in_category(cat_id) if page_action == "delete" else []
        if pages:
            # Deleting the pages must pass the same checks as deleting each
            # page on its own: the page.delete permission, protection,
            # reservations and auto-deletion schedules.  If any page fails,
            # nothing is changed, so the category never ends up half deleted.
            if not db.has_permission(user, "page.delete"):
                flash(
                    t("flash.page_delete_permission_required",
                      default="You do not have permission to delete pages."),
                    "error",
                )
                return redirect(_safe_referrer() or url_for("home"))
            blocked = [page["title"] for page in pages if _page_delete_blocker(page, user)]
            if blocked:
                flash(
                    t(
                        "flash.category_delete_pages_blocked",
                        default="The category was not deleted. These pages cannot be deleted right now because they are protected, reserved or scheduled for automatic deletion: {titles}",
                        titles=", ".join(blocked),
                    ),
                    "error",
                )
                return redirect(_safe_referrer() or url_for("home"))
        _is_docs_cat = db.is_docs_category(cat_id)
        deleted = queued = 0
        if page_action == "delete":
            deleted, queued = _delete_category_pages(cat_id, user)
            # Pages still in the category are queued for deletion (or the
            # home page).  They become uncategorized, so an admin who
            # restores one from Pending Deletions gets it back.
            db.delete_category(cat_id, page_action="uncategorize")
        else:
            db.delete_category(cat_id, page_action=page_action, target_category_id=target_cat)
        # Clear docs tracking if this was the documentation category
        if _is_docs_cat:
            db.update_site_settings(docs_category_id=None)
        log_action("delete_category", request, user=user, category_id=cat_id,
                   page_action=page_action, pages_deleted=deleted, pages_queued=queued)
        notify_change("category_delete", f"Category {cat_id} deleted")
        flash(t("flash.category_has_been_successfully_deleted"), "success")
        if queued:
            flash(t("flash.bulk_pages_queued", count=queued), "info")
        return redirect(_safe_referrer() or url_for("home"))


    @app.route("/category/<int:cat_id>/sequential-nav", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def toggle_category_sequential_nav(cat_id):
        """Toggle sequential (Prev/Next) navigation for pages in a category."""
        cat = db.get_category(cat_id)
        if not cat:
            abort(404)
        user = get_current_user()
        blocked_response = _guard_category_management_permission(
            user,
            "category.manage_sequential",
            t("flash.category_modify_no_permission"),
        )
        if blocked_response:
            return blocked_response
        enabled = request.form.get("sequential_nav", "0") == "1"
        db.update_category_sequential_nav(cat_id, enabled)
        log_action("toggle_sequential_nav", request, user=user, category_id=cat_id, enabled=enabled)
        notify_change("category_sequential_nav", f"Category {cat_id} sequential navigation {'enabled' if enabled else 'disabled'}")
        flash(t("flash.sequential_navigation_setting_updated"), "success")
        return redirect(_safe_referrer() or url_for("home"))
