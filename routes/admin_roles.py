"""Administration: roles."""

from flask import render_template, request, redirect, url_for, flash, abort
import db
from helpers import (
    login_required,
    admin_required,
    get_current_user,
    rate_limit,
    t,
)
from wiki_logger import log_action
from sync import notify_change


def register_admin_roles_routes(app):
    """Register administration routes for roles."""

    @app.route("/admin/roles")
    @login_required
    @admin_required
    def admin_roles():
        """List all custom roles."""
        roles = db.list_custom_roles()
        return render_template("admin/custom_roles.html", roles=roles)

    @app.route("/admin/roles/create", methods=["GET", "POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_create_role():
        """Create a new custom role."""
        current_user = get_current_user()
        all_categories = db.list_categories()

        from helpers._permissions import (
            group_permissions_by_category,
            get_default_permissions,
        )

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            base_role = request.form.get("base_role", "user")

            if not name:
                flash(t("flash.role_name_is_required_to_continue"), "error")
                return redirect(url_for("admin_create_role"))
            if len(name) > 100:
                flash(t("flash.role_name_cannot_exceed_100_characters"), "error")
                return redirect(url_for("admin_create_role"))
            if base_role not in ("user", "editor"):
                flash(t("flash.invalid_base_role"), "error")
                return redirect(url_for("admin_create_role"))

            selected_perms = request.form.getlist("permissions")
            read_restricted = request.form.get("read_restricted") == "1"
            read_category_ids = (
                [
                    int(v)
                    for v in request.form.getlist("read_category_ids")
                    if v.isdigit()
                ]
                if read_restricted
                else []
            )
            write_restricted = request.form.get("write_restricted") == "1"
            write_category_ids = (
                [
                    int(v)
                    for v in request.form.getlist("write_category_ids")
                    if v.isdigit()
                ]
                if write_restricted
                else []
            )

            try:
                role_id = db.create_custom_role(
                    name=name,
                    description=description,
                    base_role=base_role,
                    permission_keys=selected_perms,
                    read_restricted=read_restricted,
                    read_category_ids=read_category_ids,
                    write_restricted=write_restricted,
                    write_category_ids=write_category_ids,
                    created_by=current_user["id"],
                )
            except db.IntegrityError:
                flash(t("flash.a_role_with_that_name_already_exists"), "error")
                return redirect(url_for("admin_create_role"))

            log_action("admin_create_role", request, user=current_user, role_name=name)
            notify_change("admin_create_role", f"Custom role '{name}' created")
            flash(
                t("flash.role_name_has_been_successfully_created", name=name), "success"
            )
            return redirect(url_for("admin_edit_role", role_id=role_id))

        defaults = get_default_permissions("user")
        return render_template(
            "admin/custom_role_edit.html",
            role=None,
            role_name="",
            role_description="",
            base_role="user",
            permissions=group_permissions_by_category("user"),
            enabled_permissions=defaults,
            read_restricted=False,
            read_allowed_categories=[],
            write_restricted=False,
            write_allowed_categories=[],
            all_categories=all_categories,
            role_users=[],
            all_users=[],
            is_create=True,
        )

    @app.route("/admin/roles/<int:role_id>", methods=["GET", "POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_edit_role(role_id):
        """Edit an existing custom role."""
        role = db.get_custom_role(role_id)
        if not role:
            abort(404)

        current_user = get_current_user()
        all_categories = db.list_categories()

        from helpers._permissions import group_permissions_by_category

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            base_role = request.form.get("base_role", role["base_role"])

            if not name:
                flash(t("flash.role_name_is_required_to_continue"), "error")
                return redirect(url_for("admin_edit_role", role_id=role_id))
            if len(name) > 100:
                flash(t("flash.role_name_cannot_exceed_100_characters"), "error")
                return redirect(url_for("admin_edit_role", role_id=role_id))
            if base_role not in ("user", "editor"):
                flash(t("flash.invalid_base_role"), "error")
                return redirect(url_for("admin_edit_role", role_id=role_id))

            selected_perms = request.form.getlist("permissions")
            read_restricted = request.form.get("read_restricted") == "1"
            read_category_ids = (
                [
                    int(v)
                    for v in request.form.getlist("read_category_ids")
                    if v.isdigit()
                ]
                if read_restricted
                else []
            )
            write_restricted = request.form.get("write_restricted") == "1"
            write_category_ids = (
                [
                    int(v)
                    for v in request.form.getlist("write_category_ids")
                    if v.isdigit()
                ]
                if write_restricted
                else []
            )

            try:
                db.update_custom_role(
                    role_id,
                    name=name,
                    description=description,
                    base_role=base_role,
                    permission_keys=selected_perms,
                    read_restricted=read_restricted,
                    read_category_ids=read_category_ids,
                    write_restricted=write_restricted,
                    write_category_ids=write_category_ids,
                )
            except db.IntegrityError:
                flash(t("flash.a_role_with_that_name_already_exists"), "error")
                return redirect(url_for("admin_edit_role", role_id=role_id))

            log_action("admin_edit_role", request, user=current_user, role_name=name)
            notify_change("admin_edit_role", f"Custom role '{name}' updated")
            flash(
                t("flash.role_has_been_successfully_updated_changes_apply_to"),
                "success",
            )
            return redirect(url_for("admin_edit_role", role_id=role_id))

        role_users = db.get_users_with_role(role_id)
        all_users_list = db.list_users()
        # Filter out admins/owners and users already in this role
        role_user_ids = {u["id"] for u in role_users}
        assignable_users = [
            u
            for u in all_users_list
            if u["role"] not in ("admin", "owner") and u["id"] not in role_user_ids
        ]

        return render_template(
            "admin/custom_role_edit.html",
            role=role,
            role_name=role["name"],
            role_description=role["description"],
            base_role=role["base_role"],
            permissions=group_permissions_by_category(role["base_role"]),
            enabled_permissions=role["permission_keys"],
            read_restricted=role["read_restricted"],
            read_allowed_categories=role["read_category_ids"],
            write_restricted=role["write_restricted"],
            write_allowed_categories=role["write_category_ids"],
            all_categories=all_categories,
            role_users=role_users,
            all_users=assignable_users,
            is_create=False,
        )

    @app.route("/admin/roles/<int:role_id>/assign", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_role_assign_user(role_id):
        """Assign a user to a custom role."""
        role = db.get_custom_role(role_id)
        if not role:
            abort(404)

        current_user = get_current_user()
        user_id = request.form.get("user_id", "").strip()
        if not user_id:
            flash(t("flash.user_id_is_required"), "error")
            return redirect(url_for("admin_edit_role", role_id=role_id))

        target = db.get_user_by_id(user_id)
        if not target:
            flash(t("flash.user_not_found"), "error")
            return redirect(url_for("admin_edit_role", role_id=role_id))

        if target["role"] in ("admin", "owner"):
            flash(t("flash.admin_accounts_cannot_be_assigned_to_custom_roles"), "error")
            return redirect(url_for("admin_edit_role", role_id=role_id))
        if db.get_role_expiry(user_id) is not None:
            flash(
                t("flash.this_user_has_a_temporary_role_schedule_remove"),
                "error",
            )
            return redirect(url_for("admin_edit_role", role_id=role_id))
        if target["custom_role_id"] == role_id:
            flash(t("flash.user_is_already_assigned_to_this_role"), "info")
            return redirect(url_for("admin_edit_role", role_id=role_id))

        db.assign_custom_role(user_id, role_id)
        log_action(
            "admin_assign_role",
            request,
            user=current_user,
            target_user=target["username"],
            role_name=role["name"],
        )
        notify_change(
            "admin_assign_role",
            f"User '{target['username']}' assigned to role '{role['name']}'",
        )
        flash(
            t(
                "flash.user_username_has_been_successfully_assigned_to_role",
                username=target["username"],
                name=role["name"],
            ),
            "success",
        )
        return redirect(url_for("admin_edit_role", role_id=role_id))

    @app.route("/admin/roles/<int:role_id>/unassign", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_role_unassign_user(role_id):
        """Remove a user from a custom role."""
        role = db.get_custom_role(role_id)
        if not role:
            abort(404)

        current_user = get_current_user()
        user_id = request.form.get("user_id", "").strip()
        if not user_id:
            flash(t("flash.user_id_is_required"), "error")
            return redirect(url_for("admin_edit_role", role_id=role_id))

        target = db.get_user_by_id(user_id)
        if not target:
            flash(t("flash.user_not_found"), "error")
            return redirect(url_for("admin_edit_role", role_id=role_id))
        if target["role"] in ("admin", "owner"):
            flash(t("flash.admin_accounts_cannot_be_assigned_to_custom_roles"), "error")
            return redirect(url_for("admin_edit_role", role_id=role_id))
        if not target["custom_role_id"]:
            flash(t("flash.no_custom_role_to_remove"), "info")
            return redirect(url_for("admin_edit_role", role_id=role_id))
        if db.get_role_expiry(user_id) is not None:
            flash(
                t("flash.this_user_has_a_temporary_role_schedule_remove_838bc3"),
                "error",
            )
            return redirect(url_for("admin_edit_role", role_id=role_id))

        db.unassign_custom_role(user_id)
        log_action(
            "admin_unassign_role",
            request,
            user=current_user,
            target_user=target["username"],
            role_name=role["name"],
        )
        notify_change(
            "admin_unassign_role",
            f"User '{target['username']}' removed from role '{role['name']}'",
        )
        flash(
            t(
                "flash.user_username_has_been_removed_from_role_name",
                username=target["username"],
                name=role["name"],
            ),
            "success",
        )
        return redirect(url_for("admin_edit_role", role_id=role_id))

    @app.route("/admin/roles/<int:role_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_delete_role(role_id):
        """Delete a custom role and reassign users."""
        role = db.get_custom_role(role_id)
        if not role:
            abort(404)

        current_user = get_current_user()
        replacement_id = request.form.get("replacement_role_id", "").strip()
        replacement_role_id = (
            int(replacement_id) if replacement_id and replacement_id.isdigit() else None
        )

        db.delete_custom_role(role_id, replacement_role_id=replacement_role_id)
        log_action(
            "admin_delete_role", request, user=current_user, role_name=role["name"]
        )
        notify_change("admin_delete_role", f"Custom role '{role['name']}' deleted")
        flash(
            t(
                "flash.role_name_has_been_successfully_deleted_affected_users",
                name=role["name"],
            ),
            "success",
        )
        return redirect(url_for("admin_roles"))

    @app.route("/admin/users/<string:user_id>/assign-role", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_user_assign_role(user_id):
        """Unified route to assign a standard or custom role to a user."""
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)

        current_user = get_current_user()

        if target["role"] == "owner":
            flash(t("flash.owner_status_can_only_be_changed_by"), "error")
            return redirect(url_for("admin_users"))

        identity = request.form.get("identity", "").strip()

        # Backward compat: accept custom_role_id parameter directly
        if not identity:
            crid = request.form.get("custom_role_id", "").strip()
            if crid == "none":
                identity = "custom:none"
            elif crid:
                identity = f"custom:{crid}"

        if db.get_role_expiry(user_id) is not None:
            flash(
                t("flash.this_user_has_a_temporary_role_schedule_remove_a0a6ec"),
                "error",
            )
            return redirect(url_for("admin_users"))

        if identity.startswith("custom:"):
            role_part = identity.split(":")[1] if ":" in identity else ""
            if role_part in ("none", ""):
                # Unassign custom role
                db.unassign_custom_role(user_id)
                log_action(
                    "admin_unassign_role",
                    request,
                    user=current_user,
                    target_user=target["username"],
                )
                flash(
                    t(
                        "flash.custom_role_removed_from_username",
                        username=target["username"],
                    ),
                    "success",
                )
            else:
                try:
                    role_id = int(role_part)
                except (ValueError, IndexError):
                    flash(t("flash.invalid_custom_role_selection"), "error")
                    return redirect(url_for("admin_users"))

                role = db.get_custom_role(role_id)
                if not role:
                    flash(t("flash.selected_custom_role_does_not_exist"), "error")
                    return redirect(url_for("admin_users"))

                db.assign_custom_role(user_id, role_id)
                log_action(
                    "admin_assign_role",
                    request,
                    user=current_user,
                    target_user=target["username"],
                    role_name=role["name"],
                )
                flash(
                    t(
                        "flash.user_username_now_identifies_as_name",
                        username=target["username"],
                        name=role["name"],
                    ),
                    "success",
                )

        elif identity.startswith("std:"):
            role_name = identity.split(":")[1]
            if role_name not in ("user", "editor", "admin"):
                flash(t("flash.invalid_role_selection"), "error")
                return redirect(url_for("admin_users"))

            if user_id == current_user["id"] and role_name != current_user["role"]:
                flash(t("flash.cannot_change_your_own_role"), "error")
                return redirect(url_for("admin_users"))

            if (
                target["role"] == "owner"
                and role_name != "owner"
                and db.count_owners() <= 1
            ):
                flash(t("flash.cannot_demote_the_last_owner"), "error")
                return redirect(url_for("admin_users"))

            old_role = target["role"]
            db.update_user(user_id, role=role_name, custom_role_id=None)
            db.clear_user_permissions(user_id)  # Reset to role defaults
            db.record_role_change(
                user_id, old_role, role_name, changed_by=current_user["id"]
            )

            # Auto-withdraw pending contributions if user gained direct edit access
            if role_name in ("editor", "admin", "owner") and old_role == "user":
                withdrawn = db.auto_withdraw_contributions_for_promoted_user(user_id)
                if withdrawn:
                    flash(
                        t(
                            "flash.withdrawn_pending_contributions_autowithdrawn_user_now_has_d",
                            withdrawn=withdrawn,
                        ),
                        "info",
                    )

            log_action(
                "admin_change_role",
                request,
                user=current_user,
                target_user=target["username"],
                new_role=role_name,
            )
            flash(
                t(
                    "flash.user_username_now_identifies_as_capitalize",
                    username=target["username"],
                    capitalize=role_name.capitalize(),
                ),
                "success",
            )
        else:
            flash(t("flash.no_identity_selection_made"), "info")

        return redirect(url_for("admin_users"))
