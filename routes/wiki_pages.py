"""Wiki pages routes."""

from flask import (
    render_template, request, abort,
)
import db
from helpers import (
    login_required, get_current_user,
    user_can_view_page, render_markdown, format_datetime, time_ago, user_can_use_page_builder,
)
from wiki_logger import log_action

from .wiki_common import (
    _contribution_approval_enabled,
    _get_page_protection_context,
    _get_reservation_context,
    _user_can_edit_page,
    _user_can_propose_edit,
)


def register_wiki_pages_routes(app):
    """Register pages endpoints on the application."""

    @app.route("/")
    @login_required
    def home():
        """Render the wiki home page."""
        page = db.get_home_page()
        user = get_current_user()
        if page and not user_can_view_page(user, page):
            return render_template("wiki/navigation.html")
        content_html = render_markdown(page["content"], embed_videos=True) if page else ""
        editor_info = None
        if page and page["last_edited_by"]:
            if str(page["last_edited_by"]) == str(db.SYSTEM_USER_ID):
                editor_info = {
                    "username": "the system",
                    "time_ago": time_ago(page["last_edited_at"]),
                    "edited_at": format_datetime(page["last_edited_at"]),
                }
            else:
                editor = db.get_user_by_id(page["last_edited_by"])
                if editor:
                    editor_info = {
                        "username": editor["username"],
                        "time_ago": time_ago(page["last_edited_at"]),
                        "edited_at": format_datetime(page["last_edited_at"]),
                    }

        log_action("view_page", request, user=user, page="home")
        attachments = db.get_page_attachments(page["id"]) if page else None
        assessment = db.get_assessment_for_page(page["id"]) if page else None

        # Get user's pending contribution for this page (if contribution system enabled)
        user_contribution = None
        user_can_propose = False
        if page and user:
            if _contribution_approval_enabled() and not _user_can_edit_page(user, page):
                user_contribution = db.get_user_contribution_for_page(page["id"], user["id"])
                user_can_propose = _user_can_propose_edit(user, page)

        return render_template(
            "wiki/page.html",
            page=page,
            content_html=content_html,
            editor_info=editor_info,
            attachments=attachments,
            assessment=assessment,
            user_contribution=user_contribution,
            user_can_propose=user_can_propose,
            can_use_page_builder=user_can_use_page_builder(user),
        )


    @app.route("/page/<slug>")
    @login_required
    def view_page(slug):
        """Render a wiki page identified by its URL slug."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        # Enforce the current page-visibility rules, including category access
        # restrictions and deindexed-page permissions.
        if not user_can_view_page(user, page):
            abort(403)
        content_html = render_markdown(page["content"], embed_videos=True)
        editor_info = None
        if page["last_edited_by"]:
            if str(page["last_edited_by"]) == str(db.SYSTEM_USER_ID):
                editor_info = {
                    "username": "the system",
                    "time_ago": time_ago(page["last_edited_at"]),
                    "edited_at": format_datetime(page["last_edited_at"]),
                }
            else:
                editor = db.get_user_by_id(page["last_edited_by"])
                if editor:
                    editor_info = {
                        "username": editor["username"],
                        "time_ago": time_ago(page["last_edited_at"]),
                        "edited_at": format_datetime(page["last_edited_at"]),
                    }

        log_action("view_page", request, user=user, page=slug)
        attachments = db.get_page_attachments(page["id"])
        prev_page, next_page = db.get_adjacent_pages(page["id"])
        if prev_page and not user_can_view_page(user, prev_page):
            prev_page = None
        if next_page and not user_can_view_page(user, next_page):
            next_page = None

        # Get reservation status if user is an editor
        reservation_status = None
        reservation_context = _get_reservation_context(page, user)
        if reservation_context:
            reservation_status = reservation_context["status"]
        protection_context = _get_page_protection_context(page, user)
        assessment = db.get_assessment_for_page(page["id"])

        # Get temporary page expiry info
        page_expiry = None
        try:
            page_expiry = db.get_page_expiry(page["id"])
        except Exception:
            pass

        # Get pending deletion info for admins
        pending_deletion_info = None
        if page["pending_deletion"] and user and user["role"] in ("admin", "owner"):
            pd_info = db.get_pending_deletion_info(page["id"])
            if pd_info:
                deleted_by = db.get_user_by_id(pd_info["pending_deletion_by"]) if pd_info["pending_deletion_by"] else None
                pd_info["deleted_by_username"] = deleted_by["username"] if deleted_by else "[deleted user]"
                pending_deletion_info = pd_info

        # Get user's pending contribution for this page (if contribution system enabled)
        user_contribution = None
        if user and _contribution_approval_enabled() and not _user_can_edit_page(user, page):
            user_contribution = db.get_user_contribution_for_page(page["id"], user["id"])

        return render_template(
            "wiki/page.html",
            page=page,
            content_html=content_html,
            editor_info=editor_info,
            attachments=attachments,
            prev_page=prev_page,
            next_page=next_page,
            reservation_status=reservation_status,
            reservation_context=reservation_context,
            protection_context=protection_context,
            assessment=assessment,
            page_expiry=page_expiry,
            pending_deletion_info=pending_deletion_info,
            user_contribution=user_contribution,
            user_can_propose=_user_can_propose_edit(user, page),
            can_use_page_builder=user_can_use_page_builder(user),
        )
