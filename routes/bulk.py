"""
BananaWiki: Admin bulk-management routes.

Provides a single admin surface (``/admin/bulk``) for selecting and bulk-deleting
items across the wiki's independent services:

* Wiki categories
* Wiki pages
* Canvases (a.k.a. canvas layouts)
* Kanban boards

Each service keeps its own routes, DB layer and per-record delete code paths.
This module deliberately *reuses* the same per-record helpers (``delete_page``,
``delete_category``, ``canvas_delete_layout``, ``kanban_delete_board``) so the
underlying business rules (deletion slowdown, cascade rules, permission checks)
stay in one place.

Kanban *contents* (columns and tickets within a board) already have a rich
in-board bulk-action toolbar provided by ``app/templates/kanban/board.html``,
and canvas *contents* (nodes within a canvas) are managed by the in-canvas
multi-select shortcut on the canvas editor itself. They are intentionally not
duplicated here.
"""

from flask import render_template, request, redirect, url_for, flash

import db
from helpers import (
    login_required, admin_required, get_current_user, rate_limit, t,
)
from wiki_logger import log_action
from sync import notify_change
from routes.uploads import cleanup_unused_uploads


def _parse_int_ids(form, key):
    """Return the de-duplicated list of integer ids submitted under *key*.

    Silently drops malformed values rather than 400ing the whole request. The
    bulk surface should still process the well-formed selections even if a row
    in the listing was tampered with.
    """
    raw = form.getlist(key)
    seen = set()
    out = []
    for value in raw:
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _parse_str_ids(form, key):
    """Return the de-duplicated list of string ids submitted under *key*."""
    raw = form.getlist(key)
    seen = set()
    out = []
    for value in raw:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def register_bulk_routes(app):
    """Register the admin bulk-management page and its POST handlers."""

    @app.route("/admin/bulk")
    @login_required
    @admin_required
    def admin_bulk_manage():
        """Render the unified admin bulk-management page."""
        categories = db.list_categories()
        pages = db.list_all_pages()
        canvases = db.canvas_list_layouts()
        kanban_boards = db.kanban_list_boards()
        return render_template(
            "admin/bulk_manage.html",
            bulk_categories=categories,
            bulk_pages=pages,
            bulk_canvases=canvases,
            bulk_kanban_boards=kanban_boards,
        )

    @app.route("/admin/bulk/categories/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_bulk_delete_categories():
        """Delete the selected wiki categories.

        Pages inside each deleted category are uncategorised rather than
        cascade-deleted so the admin never silently loses content.
        """
        ids = _parse_int_ids(request.form, "ids")
        user = get_current_user()
        deleted = 0
        for cat_id in ids:
            cat = db.get_category(cat_id)
            if not cat:
                continue
            db.delete_category(cat_id, page_action="uncategorize")
            log_action(
                "delete_category", request, user=user,
                category_id=cat_id, page_action="uncategorize",
            )
            deleted += 1
        if deleted:
            flash(
                t("flash.bulk_categories_deleted", count=deleted),
                "success",
            )
        else:
            flash(t("flash.bulk_no_categories_selected"), "error")
        return redirect(url_for("admin_bulk_manage"))

    @app.route("/admin/bulk/pages/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_bulk_delete_pages():
        """Delete the selected wiki pages.

        Home pages and pages already queued for deletion are skipped to mirror
        the single-page delete route's safeguards.  When the
        ``deletion_slowdown`` plugin is enabled, eligible pages are routed
        through the grace period rather than deleted immediately.
        """
        ids = _parse_int_ids(request.form, "ids")
        user = get_current_user()
        slowdown_active = db.is_plugin_enabled("deletion_slowdown")
        site_settings = db.get_site_settings() or {}
        docs_bypass = bool(site_settings.get("docs_bypass_deletion_slowdown"))
        deleted = 0
        queued = 0
        skipped = 0
        for page_id in ids:
            page = db.get_page(page_id)
            if not page or page["is_home"] or page["pending_deletion"]:
                skipped += 1
                continue
            if db.get_page_expiry(page["id"]) is not None:
                # Page is already scheduled for automatic deletion.
                skipped += 1
                continue
            bypass = slowdown_active and db.is_docs_category(page["category_id"]) and docs_bypass
            if slowdown_active and not bypass:
                db.mark_page_pending_deletion(page["id"], user["id"])
                log_action("page_pending_deletion", request, user=user, page=page["slug"])
                notify_change(
                    "page_pending_delete",
                    f"Page '{page['slug']}' queued for deletion (48h grace period)",
                )
                queued += 1
            else:
                db.delete_page(page["id"])
                log_action("delete_page", request, user=user, page=page["slug"])
                notify_change("page_delete", f"Page '{page['slug']}' deleted")
                deleted += 1
        if deleted:
            cleanup_unused_uploads()
        if not (deleted or queued):
            flash(t("flash.bulk_no_pages_selected"), "error")
        else:
            parts = []
            if deleted:
                parts.append(t("flash.bulk_pages_deleted", count=deleted))
            if queued:
                parts.append(t("flash.bulk_pages_queued", count=queued))
            if skipped:
                parts.append(t("flash.bulk_pages_skipped", count=skipped))
            flash(", ".join(parts), "success")
        return redirect(url_for("admin_bulk_manage"))

    @app.route("/admin/bulk/canvases/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_bulk_delete_canvases():
        """Delete the selected canvas layouts and their permissions (cascade)."""
        ids = _parse_int_ids(request.form, "ids")
        user = get_current_user()
        deleted = 0
        for layout_id in ids:
            layout = db.canvas_get_layout(layout_id)
            if not layout:
                continue
            db.canvas_delete_layout(layout_id)
            log_action(
                "canvas_delete", request, user=user,
                canvas_id=layout_id, title=layout["title"],
            )
            deleted += 1
        if deleted:
            flash(t("flash.bulk_canvases_deleted", count=deleted), "success")
        else:
            flash(t("flash.bulk_no_canvases_selected"), "error")
        return redirect(url_for("admin_bulk_manage"))

    @app.route("/admin/bulk/kanban-boards/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_bulk_delete_kanban_boards():
        """Delete the selected kanban boards (cascading to columns/tickets)."""
        ids = _parse_int_ids(request.form, "ids")
        user = get_current_user()
        deleted = 0
        for board_id in ids:
            board = db.kanban_get_board(board_id)
            if not board:
                continue
            db.kanban_delete_board(board_id)
            log_action(
                "kanban_board_delete", request, user=user,
                board_id=board_id, title=board["title"],
            )
            deleted += 1
        if deleted:
            flash(t("flash.bulk_kanban_deleted", count=deleted), "success")
        else:
            flash(t("flash.bulk_no_kanban_selected"), "error")
        return redirect(url_for("admin_bulk_manage"))
