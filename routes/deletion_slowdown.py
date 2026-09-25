"""
BananaWiki: Deletion Slowdown admin routes.

Provides the admin interface for reviewing and restoring pages that are in
the 48-hour pending-deletion grace period.
"""

from flask import render_template, request, redirect, url_for, flash

import db
from helpers import login_required, admin_required, get_current_user, rate_limit
from helpers import t  # noqa: F401  (i18n)
from wiki_logger import log_action
from sync import notify_change


def register_deletion_slowdown_routes(app):
    """Register admin routes for the deletion_slowdown plugin on *app*."""

    @app.route("/admin/pending-deletions")
    @login_required
    @admin_required
    def admin_pending_deletions():
        """List all pages currently in the pending-deletion grace period."""
        pending = db.list_pending_deletions()
        return render_template(
            "admin/pending_deletions.html",
            pending=pending,
        )

    @app.route("/admin/pending-deletions/<int:page_id>/restore", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_restore_pending_deletion(page_id):
        """Restore a page from pending-deletion state, cancelling the deletion."""
        page = db.get_page(page_id)
        if not page or not page["pending_deletion"]:
            flash(t("flash.page_not_found_or_not_in_pendingdeletion_state"), "error")
            return redirect(url_for("admin_pending_deletions"))
        admin = get_current_user()
        restored = db.restore_page_from_pending_deletion(page_id)
        if restored:
            log_action("restore_pending_deletion", request, user=admin,
                       page=page["slug"], page_title=page["title"])
            notify_change("page_restored", f"Page '{page['slug']}' restored from pending deletion")
            flash(t("flash.page_title_has_been_successfully_restored", title=page['title']), "success")
            return redirect(url_for("view_page", slug=page["slug"]))
        flash(t("flash.could_not_restore_the_page_it_may_have"), "error")
        return redirect(url_for("admin_pending_deletions"))
