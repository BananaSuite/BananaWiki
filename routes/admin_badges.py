"""Administration: badges."""

from flask import render_template, request, redirect, url_for, flash, abort
import db
from helpers import (
    login_required,
    admin_required,
    get_current_user,
    _is_valid_hex_color,
    rate_limit,
    t,
)
from wiki_logger import log_action, get_logger
from sync import notify_change


def register_admin_badges_routes(app):
    """Register administration routes for badges."""

    @app.route("/admin/badges")
    @login_required
    @admin_required
    def admin_badges():
        """Admin: list all badge types and manage them."""
        badge_types = db.list_badge_types()
        # One GROUP BY for all holder counts, rather than a count query per
        # badge type.
        holder_counts = db.get_all_badge_holder_counts()
        badge_stats = [
            {"badge": badge, "holder_count": holder_counts.get(badge["id"], 0)}
            for badge in badge_types
        ]
        return render_template(
            "admin/badges.html",
            badge_stats=badge_stats,
            trigger_types=db.VALID_TRIGGER_TYPES,
        )

    @app.route("/admin/badges/create", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_create_badge():
        """Admin: create a new badge type."""
        user = get_current_user()
        name = request.form.get("name", "").strip()[:100]
        description = request.form.get("description", "").strip()[:500]
        icon = request.form.get("icon", "🏆").strip()[:10]
        color = request.form.get("color", "#ffd700").strip()
        enabled = request.form.get("enabled") == "1"
        auto_trigger = request.form.get("auto_trigger") == "1"
        trigger_type = request.form.get("trigger_type", "").strip()
        trigger_threshold = request.form.get("trigger_threshold", 0, type=int)
        allow_multiple = request.form.get("allow_multiple") == "1"

        if not name:
            flash(t("flash.a_badge_name_is_required_to_continue"), "error")
            return redirect(url_for("admin_badges"))

        if not _is_valid_hex_color(color):
            flash(t("flash.invalid_badge_color"), "error")
            return redirect(url_for("admin_badges"))

        if trigger_type not in db.VALID_TRIGGER_TYPES:
            flash(t("flash.invalid_trigger_type"), "error")
            return redirect(url_for("admin_badges"))

        try:
            badge_id = db.create_badge_type(
                name=name,
                description=description,
                icon=icon,
                color=color,
                enabled=enabled,
                auto_trigger=auto_trigger,
                trigger_type=trigger_type,
                trigger_threshold=trigger_threshold,
                allow_multiple=allow_multiple,
                created_by=user["id"],
            )
            log_action("create_badge_type", request, user=user, badge_id=badge_id)
            notify_change("badge_create", f"Badge type '{name}' created")
            flash(t("flash.badge_name_created_successfully", name=name), "success")
        except Exception as e:
            get_logger().exception("Badge creation failed: %s", e)
            flash(t("flash.an_error_occurred_while_saving_the_badge_please"), "error")

        return redirect(url_for("admin_badges"))

    @app.route("/admin/badges/<int:badge_id>/edit", methods=["GET", "POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_edit_badge(badge_id):
        """Admin: edit a badge type."""
        badge = db.get_badge_type(badge_id)
        if not badge:
            abort(404)
        user = get_current_user()

        if request.method == "GET":
            holders = db.get_badge_holders(badge_id, include_revoked=False)
            return render_template(
                "admin/edit_badge.html",
                badge=badge,
                holders=holders,
                trigger_types=db.VALID_TRIGGER_TYPES,
            )

        # POST - update badge
        action = request.form.get("action", "")

        if action == "update":
            name = request.form.get("name", "").strip()[:100]
            description = request.form.get("description", "").strip()[:500]
            icon = request.form.get("icon", "🏆").strip()[:10]
            color = request.form.get("color", "#ffd700").strip()
            enabled = request.form.get("enabled") == "1"
            auto_trigger = request.form.get("auto_trigger") == "1"
            trigger_type = request.form.get("trigger_type", "").strip()
            trigger_threshold = request.form.get("trigger_threshold", 0, type=int)
            allow_multiple = request.form.get("allow_multiple") == "1"

            if not name:
                flash(t("flash.a_badge_name_is_required_to_continue"), "error")
                return redirect(url_for("admin_edit_badge", badge_id=badge_id))

            if not _is_valid_hex_color(color):
                flash(t("flash.invalid_badge_color"), "error")
                return redirect(url_for("admin_edit_badge", badge_id=badge_id))

            if trigger_type not in db.VALID_TRIGGER_TYPES:
                flash(t("flash.invalid_trigger_type"), "error")
                return redirect(url_for("admin_edit_badge", badge_id=badge_id))

            try:
                db.update_badge_type(
                    badge_id,
                    name=name,
                    description=description,
                    icon=icon,
                    color=color,
                    enabled=enabled,
                    auto_trigger=auto_trigger,
                    trigger_type=trigger_type,
                    trigger_threshold=trigger_threshold,
                    allow_multiple=allow_multiple,
                )
                log_action("update_badge_type", request, user=user, badge_id=badge_id)
                notify_change("badge_update", f"Badge type '{name}' updated")
                flash(t("flash.badge_name_updated_successfully", name=name), "success")
            except Exception as e:
                get_logger().exception("Badge update failed: %s", e)
                flash(
                    t("flash.an_error_occurred_while_saving_the_badge_please"), "error"
                )

            return redirect(url_for("admin_edit_badge", badge_id=badge_id))

        elif action == "delete":
            # Delete badge type and optionally all user badges
            remove_user_badges = request.form.get("remove_user_badges") == "1"
            db.delete_badge_type(badge_id, remove_user_badges=remove_user_badges)
            log_action("delete_badge_type", request, user=user, badge_id=badge_id)
            notify_change("badge_delete", "Badge type deleted")
            flash(t("flash.badge_type_deleted"), "success")
            return redirect(url_for("admin_badges"))

        return redirect(url_for("admin_edit_badge", badge_id=badge_id))

    @app.route("/admin/badges/<int:badge_id>/award", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_award_badge(badge_id):
        """Admin: manually award a badge to a user."""
        badge = db.get_badge_type(badge_id)
        if not badge:
            abort(404)
        user = get_current_user()
        target_username = request.form.get("username", "").strip()

        if not target_username:
            flash(t("flash.a_username_is_required_to_continue"), "error")
            return redirect(url_for("admin_edit_badge", badge_id=badge_id))

        target_user = db.get_user_by_username(target_username)
        if not target_user:
            flash(
                t(
                    "flash.user_targetusername_was_not_found",
                    target_username=target_username,
                ),
                "error",
            )
            return redirect(url_for("admin_edit_badge", badge_id=badge_id))

        result = db.award_badge(target_user["id"], badge_id, awarded_by=user["id"])
        if result is None:
            flash(
                t(
                    "flash.user_targetusername_already_has_this_badge",
                    target_username=target_username,
                ),
                "info",
            )
        else:
            log_action(
                "award_badge",
                request,
                user=user,
                target_user=target_username,
                badge_id=badge_id,
            )
            notify_change(
                "badge_award", f"Badge '{badge['name']}' awarded to '{target_username}'"
            )
            flash(
                t(
                    "flash.badge_name_awarded_to_targetusername",
                    name=badge["name"],
                    target_username=target_username,
                ),
                "success",
            )

        return redirect(url_for("admin_edit_badge", badge_id=badge_id))

    @app.route("/admin/badges/<int:badge_id>/revoke", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_revoke_badge(badge_id):
        """Admin: revoke a badge from a user."""
        badge = db.get_badge_type(badge_id)
        if not badge:
            abort(404)
        user = get_current_user()
        target_username = request.form.get("username", "").strip()
        permanent = request.form.get("permanent") == "1"

        if not target_username:
            flash(t("flash.a_username_is_required_to_continue"), "error")
            return redirect(url_for("admin_edit_badge", badge_id=badge_id))

        target_user = db.get_user_by_username(target_username)
        if not target_user:
            flash(
                t(
                    "flash.user_targetusername_was_not_found",
                    target_username=target_username,
                ),
                "error",
            )
            return redirect(url_for("admin_edit_badge", badge_id=badge_id))

        db.revoke_badge(
            target_user["id"], badge_id, revoked_by=user["id"], permanent=permanent
        )
        action_type = "permanently" if permanent else "temporarily"
        log_action(
            "revoke_badge",
            request,
            user=user,
            target_user=target_username,
            badge_id=badge_id,
        )
        notify_change(
            "badge_revoke", f"Badge '{badge['name']}' revoked from '{target_username}'"
        )
        flash(
            t(
                "flash.badge_name_actiontype_revoked_from_targetusername",
                name=badge["name"],
                action_type=action_type,
                target_username=target_username,
            ),
            "success",
        )

        return redirect(url_for("admin_edit_badge", badge_id=badge_id))

    @app.route("/admin/badges/<int:badge_id>/revoke-all", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_revoke_all_badges(badge_id):
        """Admin: revoke this badge from all users."""
        badge = db.get_badge_type(badge_id)
        if not badge:
            abort(404)
        user = get_current_user()
        permanent = request.form.get("permanent") == "1"

        db.revoke_all_badges_for_type(
            badge_id, revoked_by=user["id"], permanent=permanent
        )
        action_type = "permanently" if permanent else "temporarily"
        log_action("revoke_all_badges", request, user=user, badge_id=badge_id)
        notify_change(
            "badge_revoke_all", f"All instances of badge '{badge['name']}' revoked"
        )
        flash(
            t(
                "flash.badge_name_actiontype_revoked_from_all_users",
                name=badge["name"],
                action_type=action_type,
            ),
            "success",
        )

        return redirect(url_for("admin_edit_badge", badge_id=badge_id))
