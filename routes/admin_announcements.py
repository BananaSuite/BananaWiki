"""Administration: announcements."""

from flask import render_template, request, redirect, url_for, flash, abort
import db
from helpers import (
    login_required,
    admin_required,
    get_current_user,
    _is_valid_hex_color,
    rate_limit,
    render_markdown,
    local_datetime_to_utc,
    t,
)
from wiki_logger import log_action
from sync import notify_change


def register_admin_announcements_routes(app):
    """Register administration routes for announcements."""
    _VALID_ANN_COLORS = {"red", "orange", "yellow", "blue", "green"}
    _VALID_ANN_SIZES = {"small", "normal", "large"}
    _VALID_ANN_VISIBILITY = {"logged_in", "logged_out", "both"}
    _VALID_ANN_AUDIENCES = {"all", "allowlist", "denylist"}

    def _announcement_form_data():
        """Validate and normalize the shared announcement create/edit fields."""
        content = request.form.get("content", "").strip()
        color = request.form.get("color", "orange")
        text_size = request.form.get("text_size", "normal")
        visibility = request.form.get("visibility", "both")
        audience_mode = request.form.get("audience_mode", "all")
        audience_user_ids = list(
            dict.fromkeys(request.form.getlist("audience_user_ids"))
        )
        expires_at = request.form.get("expires_at", "").strip() or None

        if not content:
            flash(t("flash.announcement_content_is_required_to_continue"), "error")
            return None
        if len(content) > 2000:
            flash(
                t("flash.announcement_content_cannot_exceed_2000_characters"), "error"
            )
            return None
        if color not in _VALID_ANN_COLORS:
            flash(t("flash.the_specified_color_is_invalid"), "error")
            return None
        if text_size not in _VALID_ANN_SIZES:
            flash(t("flash.invalid_text_size"), "error")
            return None
        if visibility not in _VALID_ANN_VISIBILITY:
            flash(t("flash.the_specified_visibility_is_invalid"), "error")
            return None
        if audience_mode not in _VALID_ANN_AUDIENCES:
            flash(
                t(
                    "flash.invalid_announcement_audience",
                    default="The selected audience is invalid.",
                ),
                "error",
            )
            return None

        valid_user_ids = {row["id"] for row in db.list_users()}
        if any(user_id not in valid_user_ids for user_id in audience_user_ids):
            flash(
                t(
                    "flash.invalid_announcement_audience",
                    default="The selected audience is invalid.",
                ),
                "error",
            )
            return None
        if audience_mode != "all" and not audience_user_ids:
            flash(
                t(
                    "flash.announcement_audience_required",
                    default="Select at least one user for this audience.",
                ),
                "error",
            )
            return None
        if audience_mode == "all":
            audience_user_ids = []

        custom_background = None
        custom_text_color = None
        if request.form.get("custom_colors"):
            custom_background = request.form.get("custom_background", "").strip()
            custom_text_color = request.form.get("custom_text_color", "").strip()
            if not (
                _is_valid_hex_color(custom_background)
                and _is_valid_hex_color(custom_text_color)
            ):
                flash(t("flash.the_specified_color_is_invalid"), "error")
                return None

        if expires_at:
            try:
                expires_at = local_datetime_to_utc(expires_at)
            except ValueError:
                flash(t("flash.invalid_expiration_date_format"), "error")
                return None

        return {
            "content": content,
            "color": color,
            "text_size": text_size,
            "visibility": visibility,
            "expires_at": expires_at,
            "not_removable": 1 if request.form.get("not_removable") else 0,
            "show_countdown": 1 if request.form.get("show_countdown") else 0,
            "audience_mode": audience_mode,
            "audience_user_ids": audience_user_ids,
            "custom_background": custom_background,
            "custom_text_color": custom_text_color,
        }

    @app.route("/admin/announcements")
    @login_required
    @admin_required
    def admin_announcements():
        """Display the admin announcement management page."""
        announcements = db.list_announcements()
        users = db.list_users()
        target_ids = {
            ann["id"]: set(db.get_announcement_audience_user_ids(ann["id"]))
            for ann in announcements
        }
        return render_template(
            "admin/announcements.html",
            announcements=announcements,
            users=users,
            announcement_target_ids=target_ids,
        )

    @app.route("/admin/announcements/create", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_create_announcement():
        """Create a new site-wide announcement banner."""
        form_data = _announcement_form_data()
        if form_data is None:
            return redirect(url_for("admin_announcements"))
        user = get_current_user()

        db.create_announcement(user_id=user["id"], **form_data)
        log_action("create_announcement", request, user=user)
        notify_change("announcement_create", "Announcement created")
        flash(t("flash.announcement_created"), "success")
        return redirect(url_for("admin_announcements"))

    @app.route("/admin/announcements/<int:ann_id>/edit", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_edit_announcement(ann_id):
        """Edit an existing announcement's content, color, size, visibility, and expiry."""
        ann = db.get_announcement(ann_id)
        if not ann:
            abort(404)
        form_data = _announcement_form_data()
        if form_data is None:
            return redirect(url_for("admin_announcements"))
        form_data["is_active"] = 1 if request.form.get("is_active") else 0
        user = get_current_user()

        db.update_announcement(ann_id, **form_data)
        log_action("edit_announcement", request, user=user, ann_id=ann_id)
        notify_change("announcement_edit", f"Announcement {ann_id} updated")
        flash(t("flash.announcement_updated"), "success")
        return redirect(url_for("admin_announcements"))

    @app.route("/admin/announcements/<int:ann_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_delete_announcement(ann_id):
        """Permanently delete an announcement by ID."""
        ann = db.get_announcement(ann_id)
        if not ann:
            abort(404)
        user = get_current_user()
        db.delete_announcement(ann_id)
        log_action("delete_announcement", request, user=user, ann_id=ann_id)
        notify_change("announcement_delete", f"Announcement {ann_id} deleted")
        flash(t("flash.announcement_deleted"), "success")
        return redirect(url_for("admin_announcements"))

    @app.route("/announcements/<int:ann_id>")
    def view_announcement(ann_id):
        """Public route: display a single announcement's full content page."""
        user = get_current_user()
        ann = db.get_visible_announcement(
            ann_id,
            bool(user),
            user["id"] if user else None,
        )
        if not ann:
            abort(404)
        content_html = render_markdown(ann["content"])
        return render_template(
            "wiki/announcement.html", ann=ann, content_html=content_html
        )
