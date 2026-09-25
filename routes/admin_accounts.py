"""Administration: accounts."""

from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    send_file,
    abort,
)
import os, uuid
from datetime import datetime, timezone
from PIL import Image
import db
import config
from bananawiki_sdk import emit_hook
from helpers import (
    login_required,
    admin_required,
    get_current_user,
    _is_valid_hex_color,
    _is_valid_username,
    rate_limit,
    allowed_file,
    _safe_ext,
    safe_unlink_in,
    normalize_birth_date,
    MAX_SUSPEND_REASON_LENGTH,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    t,
)
from helpers._passwords import generate_password_hash
from helpers._auth_sessions import current_user_session_id
from wiki_logger import log_action
from sync import notify_change, notify_file_upload, notify_file_deleted
from routes.users import build_user_export_zip


def _revoke_api_tokens_for_password_reset(user_id, reason):
    """Revoke the API tokens of *user_id* after an admin set a new password.

    An admin reset usually follows a lost or leaked password.  Signing the
    user's browser sessions out is not enough if a token taken with the old
    password keeps working, so the tokens go too.  Returns how many were
    revoked, for the audit log.
    """
    return db.revoke_user_api_service_tokens(user_id, reason)


def register_admin_accounts_routes(app):
    """Register administration routes for accounts."""

    def _read_user_audit_log(username, max_entries=200):
        """Read the most recent log entries for a specific user from the log file.

        Reads the file from the end in 32 KB chunks so that large log files
        on long-running instances are not fully scanned when only the last
        ``max_entries`` matching lines are needed.
        """
        log_file = config.LOG_FILE
        if not os.path.exists(log_file):
            return []
        search_terms = (
            f"user={username} ",
            f"impersonated_by={username} ",
        )
        entries = []
        CHUNK_SIZE = 1 << 15  # 32 KB
        try:
            with open(log_file, "rb") as f:
                f.seek(0, 2)  # seek to end of file
                remaining = f.tell()
                # carry holds the incomplete line fragment from the start of
                # the previously read (newer) chunk so it can be completed
                # by the next (older) chunk.
                carry = b""
                while remaining > 0 and len(entries) < max_entries:
                    read_size = min(CHUNK_SIZE, remaining)
                    remaining -= read_size
                    f.seek(remaining)
                    chunk = f.read(read_size) + carry
                    lines = chunk.split(b"\n")
                    # lines[0] may be a partial line whose prefix lives in an
                    # older chunk; carry it forward to the next iteration.
                    carry = lines[0]
                    # Process complete lines in newest-first order
                    for line in reversed(lines[1:]):
                        decoded = line.decode("utf-8", errors="replace").strip()
                        if any(term in decoded for term in search_terms):
                            entries.append(decoded)
                            if len(entries) >= max_entries:
                                break
                # Handle the very first line of the file (no preceding newline)
                if len(entries) < max_entries and carry:
                    decoded = carry.decode("utf-8", errors="replace").strip()
                    if any(term in decoded for term in search_terms):
                        entries.append(decoded)
            # entries are already in newest-first order
            return entries
        except OSError:
            return []

    @app.route("/admin/users/<string:user_id>/profile", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_moderate_profile(user_id):
        """Admin: edit or disable a user's profile page."""
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)
        current_user = get_current_user()
        action = request.form.get("action", "")

        if target["is_superuser"]:
            flash(t("flash.this_account_is_protected_and_cannot_be_modified"), "error")
            return redirect(url_for("admin_users"))

        if target["role"] == "owner" and str(user_id) != str(current_user["id"]):
            flash(t("flash.owner_accounts_can_only_be_edited_by"), "error")
            return redirect(url_for("admin_users"))

        if action == "edit_profile":
            real_name = request.form.get("real_name", "").strip()[:100]
            bio = request.form.get("bio", "").strip()[:500]
            try:
                birth_date = normalize_birth_date(request.form.get("birth_date", ""))
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("user_profile", username=target["username"]))
            avatar_file = request.files.get("avatar")
            profile = db.get_user_profile(user_id)
            old_avatar = profile["avatar_filename"] if profile else ""
            new_avatar = old_avatar
            if avatar_file and avatar_file.filename:
                if not allowed_file(avatar_file.filename):
                    flash(t("flash.invalid_avatar_file_type"), "error")
                    return redirect(
                        url_for("user_profile", username=target["username"])
                    )
                avatar_file.stream.seek(0, 2)
                size = avatar_file.stream.tell()
                avatar_file.stream.seek(0)
                if size > 1 * 1024 * 1024:
                    flash(t("flash.avatar_file_size_cannot_exceed_1_mb"), "error")
                    return redirect(
                        url_for("user_profile", username=target["username"])
                    )
                try:
                    img = Image.open(avatar_file.stream)
                    img.verify()
                    avatar_file.stream.seek(0)
                except Exception:
                    flash(t("flash.avatar_is_not_a_valid_image"), "error")
                    return redirect(
                        url_for("user_profile", username=target["username"])
                    )
                avatar_dir = os.path.join(config.UPLOAD_FOLDER, "avatars")
                os.makedirs(avatar_dir, exist_ok=True)
                ext = _safe_ext(avatar_file.filename)
                if not ext:
                    flash(t("flash.invalid_file_extension"), "error")
                    return redirect(
                        url_for("user_profile", username=target["username"])
                    )
                new_avatar = f"avatars/{uuid.uuid4().hex}.{ext}"
                save_path = os.path.abspath(
                    os.path.join(config.UPLOAD_FOLDER, new_avatar)
                )
                if os.path.commonpath(
                    [os.path.abspath(config.UPLOAD_FOLDER), save_path]
                ) != os.path.abspath(config.UPLOAD_FOLDER):
                    flash(t("flash.invalid_upload_path"), "error")
                    return redirect(
                        url_for("user_profile", username=target["username"])
                    )
                try:
                    avatar_file.save(save_path)
                except OSError:
                    flash(t("flash.failed_to_save_avatar_file"), "error")
                    return redirect(
                        url_for("user_profile", username=target["username"])
                    )
                notify_file_upload(
                    new_avatar,
                    save_path,
                    display_name=f"Avatar for {target['username']}",
                )
                if old_avatar and old_avatar != new_avatar:
                    safe_unlink_in(config.UPLOAD_FOLDER, old_avatar)
                    notify_file_deleted(old_avatar)
            db.upsert_user_profile(
                user_id,
                real_name=real_name,
                bio=bio,
                birth_date=birth_date,
                avatar_filename=new_avatar,
            )
            if db.is_plugin_enabled("user_profiles"):
                profile_fields_data = request.form.getlist("profile_field_key")
                profile_fields_visible = request.form.getlist("profile_field_visible")
                visible_set = set(profile_fields_visible)
                for key in profile_fields_data:
                    value = request.form.get(f"profile_value_{key}", "").strip()
                    visible = key in visible_set
                    try:
                        db.save_user_field_value(user_id, key, value, visible)
                    except Exception:
                        pass
            log_action(
                "admin_edit_profile",
                request,
                user=current_user,
                target_user=target["username"],
            )
            notify_change(
                "admin_edit_profile", f"Profile of '{target['username']}' edited"
            )
            flash(t("flash.profile_has_been_successfully_updated"), "success")

        elif action == "remove_avatar":
            profile = db.get_user_profile(user_id)
            if profile and profile["avatar_filename"]:
                safe_unlink_in(config.UPLOAD_FOLDER, profile["avatar_filename"])
                notify_file_deleted(profile["avatar_filename"])
                db.upsert_user_profile(user_id, avatar_filename="")
            log_action(
                "admin_remove_avatar",
                request,
                user=current_user,
                target_user=target["username"],
            )
            notify_change(
                "admin_remove_avatar", f"Avatar removed for '{target['username']}'"
            )
            flash(t("flash.avatar_has_been_successfully_removed"), "success")

        elif action == "disable_profile":
            db.upsert_user_profile(
                user_id, page_disabled_by_admin=True, page_published=False
            )
            log_action(
                "admin_disable_profile",
                request,
                user=current_user,
                target_user=target["username"],
            )
            notify_change(
                "admin_disable_profile", f"Profile of '{target['username']}' disabled"
            )
            flash(t("flash.user_profile_has_been_successfully_disabled"), "success")

        elif action == "enable_profile":
            db.upsert_user_profile(user_id, page_disabled_by_admin=False)
            log_action(
                "admin_enable_profile",
                request,
                user=current_user,
                target_user=target["username"],
            )
            notify_change(
                "admin_enable_profile", f"Profile of '{target['username']}' re-enabled"
            )
            flash(t("flash.user_profile_has_been_successfully_reenabled"), "success")

        elif action == "publish_profile":
            db.upsert_user_profile(user_id, page_published=True)
            log_action(
                "admin_publish_profile",
                request,
                user=current_user,
                target_user=target["username"],
            )
            notify_change(
                "admin_publish_profile",
                f"Profile of '{target['username']}' published by admin",
            )
            flash(t("flash.user_profile_has_been_successfully_published"), "success")

        elif action == "unpublish_profile":
            db.upsert_user_profile(user_id, page_published=False)
            log_action(
                "admin_unpublish_profile",
                request,
                user=current_user,
                target_user=target["username"],
            )
            notify_change(
                "admin_unpublish_profile",
                f"Profile of '{target['username']}' unpublished by admin",
            )
            flash(t("flash.user_profile_has_been_successfully_unpublished"), "success")

        elif action == "delete_profile":
            profile = db.get_user_profile(user_id)
            if profile and profile["avatar_filename"]:
                safe_unlink_in(config.UPLOAD_FOLDER, profile["avatar_filename"])
                notify_file_deleted(profile["avatar_filename"])
            db.delete_user_profile(user_id)
            log_action(
                "admin_delete_profile",
                request,
                user=current_user,
                target_user=target["username"],
            )
            notify_change(
                "admin_delete_profile", f"Profile of '{target['username']}' deleted"
            )
            flash(t("flash.profile_has_been_successfully_deleted"), "success")

        return redirect(url_for("user_profile", username=target["username"]))

    @app.route("/admin/users/<string:user_id>/tags", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_manage_user_tags(user_id):
        """Admin: add, update, delete, or reorder custom tags for a user."""
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)
        current_user = get_current_user()
        action = request.form.get("action", "")

        if target["is_superuser"]:
            flash(t("flash.this_account_is_protected_and_cannot_be_modified"), "error")
            return redirect(url_for("user_profile", username=target["username"]))

        if target["role"] == "owner" and user_id != current_user["id"]:
            flash(t("flash.owner_accounts_can_only_be_edited_by"), "error")
            return redirect(url_for("user_profile", username=target["username"]))

        if action == "add_tag":
            label = request.form.get("tag_label", "").strip()[:50]
            color = request.form.get("tag_color", "#9b59b6").strip()
            if not label:
                flash(t("flash.a_tag_label_is_required_to_continue"), "error")
            elif not _is_valid_hex_color(color):
                flash(t("flash.the_specified_tag_color_is_invalid"), "error")
            else:
                db.add_user_custom_tag(user_id, label, color)
                log_action(
                    "admin_add_user_tag",
                    request,
                    user=current_user,
                    target_user=target["username"],
                )
                flash(t("flash.tag_has_been_successfully_added"), "success")

        elif action == "update_tag":
            tag_id = request.form.get("tag_id", type=int)
            tag = db.get_user_custom_tag(tag_id) if tag_id and tag_id > 0 else None
            if not tag or tag["user_id"] != user_id:
                flash(t("flash.the_specified_tag_was_not_found"), "error")
            else:
                label = request.form.get("tag_label", "").strip()[:50]
                color = request.form.get("tag_color", "").strip()
                if not label:
                    flash(t("flash.a_tag_label_is_required_to_continue"), "error")
                elif not _is_valid_hex_color(color):
                    flash(t("flash.the_specified_tag_color_is_invalid"), "error")
                else:
                    db.update_user_custom_tag(tag_id, label=label, color=color)
                    log_action(
                        "admin_update_user_tag",
                        request,
                        user=current_user,
                        target_user=target["username"],
                    )
                    flash(t("flash.tag_has_been_successfully_updated"), "success")

        elif action == "delete_tag":
            tag_id = request.form.get("tag_id", type=int)
            tag = db.get_user_custom_tag(tag_id) if tag_id and tag_id > 0 else None
            if not tag or tag["user_id"] != user_id:
                flash(t("flash.the_specified_tag_was_not_found"), "error")
            else:
                db.delete_user_custom_tag(tag_id)
                log_action(
                    "admin_delete_user_tag",
                    request,
                    user=current_user,
                    target_user=target["username"],
                )
                flash(t("flash.tag_has_been_successfully_deleted"), "success")

        elif action == "reorder_tags":
            order_str = request.form.get("tag_order", "")
            try:
                tag_ids = [int(x) for x in order_str.split(",") if x.strip()]
            except ValueError:
                tag_ids = []
            if tag_ids:
                # Validate all tag IDs belong to this user
                user_tags = db.get_user_custom_tags(user_id)
                valid_ids = {t["id"] for t in user_tags}
                if all(tid in valid_ids for tid in tag_ids):
                    db.reorder_user_custom_tags(user_id, tag_ids)
                    log_action(
                        "admin_reorder_user_tags",
                        request,
                        user=current_user,
                        target_user=target["username"],
                    )
                    flash(t("flash.tag_order_has_been_successfully_updated"), "success")
                else:
                    flash(t("flash.invalid_tag_ids"), "error")

        return redirect(url_for("user_profile", username=target["username"]))

    @app.route("/admin/users/<string:user_id>/attributions", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_manage_attributions(user_id):
        """Admin: deattribute contributions or delete role history entries for a user."""
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)
        current_user = get_current_user()
        action = request.form.get("action", "")

        if target["is_superuser"]:
            flash(t("flash.this_account_is_protected_and_cannot_be_modified"), "error")
            return redirect(url_for("user_profile", username=target["username"]))

        if target["role"] == "owner" and user_id != current_user["id"]:
            flash(t("flash.owner_accounts_can_only_be_edited_by"), "error")
            return redirect(url_for("user_profile", username=target["username"]))

        if action == "deattribute_contribution":
            entry_id = request.form.get("entry_id", type=int)
            if not entry_id or entry_id < 1:
                flash(t("flash.invalid_entry"), "error")
            else:
                entry = db.get_history_entry(entry_id)
                if not entry or entry["edited_by"] != user_id:
                    flash(t("flash.entry_was_not_found_or_does_not_belong"), "error")
                else:
                    db.deattribute_contribution(entry_id)
                    log_action(
                        "admin_deattribute_contribution",
                        request,
                        user=current_user,
                        target_user=target["username"],
                        entry_id=entry_id,
                    )
                    flash(t("flash.contribution_deattributed"), "success")

        elif action == "deattribute_all":
            count = db.deattribute_all_user_contributions(user_id)
            log_action(
                "admin_deattribute_all",
                request,
                user=current_user,
                target_user=target["username"],
                count=count,
            )
            notify_change(
                "admin_deattribute_all",
                f"All contributions ({count}) deattributed from '{target['username']}'",
            )
            flash(t("flash.deattributed_count_contributions", count=count), "success")

        elif action == "mass_reattribute":
            to_user_id = request.form.get("to_user_id", "").strip()
            to_user = db.get_user_by_id(to_user_id) if to_user_id else None
            if not to_user:
                flash(t("flash.invalid_target_user"), "error")
            elif to_user_id == user_id:
                flash(t("flash.cannot_reattribute_to_the_same_user"), "error")
            else:
                count = db.mass_reattribute_contributions(user_id, to_user_id)
                log_action(
                    "admin_mass_reattribute",
                    request,
                    user=current_user,
                    from_user=target["username"],
                    to_user=to_user["username"],
                    count=count,
                )
                notify_change(
                    "admin_mass_reattribute",
                    f"Mass reattribution: {count} entries from '{target['username']}' to '{to_user['username']}'",
                )
                flash(
                    t(
                        "flash.reattributed_count_contributions_to_username",
                        count=count,
                        username=to_user["username"],
                    ),
                    "success",
                )

        elif action == "delete_role_history_entry":
            entry_id = request.form.get("entry_id", type=int)
            if not entry_id or entry_id < 1:
                flash(t("flash.invalid_entry"), "error")
            else:
                rh = db.get_role_history_entry(entry_id)
                if not rh or rh["user_id"] != user_id:
                    flash(t("flash.role_history_entry_not_found"), "error")
                else:
                    db.delete_role_history_entry(entry_id)
                    log_action(
                        "admin_delete_role_history_entry",
                        request,
                        user=current_user,
                        target_user=target["username"],
                        entry_id=entry_id,
                    )
                    flash(t("flash.role_history_entry_deleted"), "success")

        elif action == "delete_all_role_history":
            count = db.delete_all_role_history(user_id)
            log_action(
                "admin_delete_all_role_history",
                request,
                user=current_user,
                target_user=target["username"],
                count=count,
            )
            flash(t("flash.deleted_count_role_history_entries", count=count), "success")

        return redirect(url_for("user_profile", username=target["username"]))

    @app.route("/admin/users")
    @login_required
    @admin_required
    def admin_users():
        """List all registered users with optional role, status, and approval filters."""
        role_filter = request.args.get("role")
        status_filter = request.args.get("status")
        approval_filter = request.args.get("approval")
        # Auto-unsuspend users whose timed suspension has elapsed so the list
        # always reflects the current state without waiting for the weekly cleanup.
        db.cleanup_expired_suspensions()
        users = db.list_users(
            role_filter=role_filter,
            status_filter=status_filter,
            approval_filter=approval_filter,
        )
        pending_count = db.count_pending_users()
        pending_quota_count = (
            db.count_pending_reservation_quota_requests()
            + db.count_pending_contribution_quota_requests()
        )
        custom_roles = db.list_custom_roles()
        userbot_tokens = {u["id"]: db.get_userbot_token_info(u["id"]) for u in users}
        return render_template(
            "admin/users.html",
            users=users,
            role_filter=role_filter,
            status_filter=status_filter,
            approval_filter=approval_filter,
            pending_count=pending_count,
            pending_quota_count=pending_quota_count,
            custom_roles=custom_roles,
            userbot_tokens=userbot_tokens,
        )

    @app.route(
        "/admin/users/<string:user_id>/reservation-quota", methods=["GET", "POST"]
    )
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_user_reservation_quota(user_id):
        """Review reservation quota history and pending requests for a user."""
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)

        current_user = get_current_user()
        if request.method == "POST":
            action = request.form.get("action", "")

            # Admin directly sets a user's quota
            if action == "set_quota":
                unlimited = request.form.get("unlimited") == "1"
                if unlimited:
                    new_quota = -1
                else:
                    new_quota = request.form.get("new_quota", type=int)
                    if new_quota is None or new_quota < 1:
                        flash(
                            t("flash.a_valid_quota_amount_is_required_to_continue"),
                            "error",
                        )
                        return redirect(
                            url_for("admin_user_reservation_quota", user_id=user_id)
                        )
                try:
                    db.set_user_reserved_pages_quota(user_id, new_quota)
                except Exception as exc:
                    flash(str(exc), "error")
                    return redirect(
                        url_for("admin_user_reservation_quota", user_id=user_id)
                    )

                label = "Unlimited" if new_quota == -1 else str(new_quota)
                log_action(
                    "admin_set_user_quota",
                    request,
                    user=current_user,
                    target_user=target["username"],
                    new_quota=label,
                )
                notify_change(
                    "admin_set_user_quota",
                    f"Admin set reservation quota for '{target['username']}' to {label}",
                )
                flash(
                    t(
                        "flash.reservation_quota_for_username_has_been_successfully_updated",
                        username=target["username"],
                        label=label,
                    ),
                    "success",
                )
                return redirect(
                    url_for("admin_user_reservation_quota", user_id=user_id)
                )

            # Approve / deny a pending quota request
            request_id = request.form.get("request_id", type=int)
            request_row = (
                db.get_reservation_quota_request(request_id) if request_id else None
            )
            if not request_row or request_row["user_id"] != user_id:
                flash(t("flash.the_specified_quota_request_was_not_found"), "error")
                return redirect(
                    url_for("admin_user_reservation_quota", user_id=user_id)
                )

            if action not in {"approve_request", "deny_request"}:
                flash(t("flash.invalid_quota_review_action"), "error")
                return redirect(
                    url_for("admin_user_reservation_quota", user_id=user_id)
                )

            try:
                reviewed_request = db.review_reservation_quota_request(
                    request_id,
                    current_user["id"],
                    approved=action == "approve_request",
                    review_reason=request.form.get("review_reason", ""),
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(
                    url_for("admin_user_reservation_quota", user_id=user_id)
                )

            log_action(
                "review_reservation_quota_request",
                request,
                user=current_user,
                target_user=target["username"],
                decision=reviewed_request["status"],
                requested_quota=reviewed_request["requested_quota"],
            )
            notify_change(
                "reservation_quota_request_review",
                f"Reservation quota request for '{target['username']}' was {reviewed_request['status']}",
            )
            if reviewed_request["status"] == "approved":
                flash(
                    t("flash.quota_request_has_been_successfully_approved"), "success"
                )
            else:
                flash(t("flash.quota_request_has_been_successfully_denied"), "success")
            return redirect(url_for("admin_user_reservation_quota", user_id=user_id))

        return render_template(
            "account/reservation_quota.html",
            target_user=target,
            current_quota=db.get_effective_reserved_pages_quota(user_id),
            default_quota=db.get_default_reserved_pages_quota(),
            active_reservation_count=db.get_user_active_reservation_count(user_id),
            pending_request=db.get_pending_reservation_quota_request(user_id),
            quota_requests=db.list_reservation_quota_requests(user_id),
            max_reason_length=db.MAX_QUOTA_REQUEST_REASON_LENGTH,
            max_review_reason_length=db.MAX_QUOTA_REVIEW_REASON_LENGTH,
            admin_view=True,
        )

    @app.route("/admin/users/<string:user_id>/edit", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_edit_user(user_id):
        """Process all user-management actions (role change, suspend, password reset, etc.) for a single user."""
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)
        action = request.form.get("action", "")
        current_user = get_current_user()

        if target["is_superuser"]:
            flash(t("flash.this_account_is_protected_and_cannot_be_modified"), "error")
            return redirect(url_for("admin_users"))
        if action == "change_username":
            if target["role"] == "owner" and user_id != current_user["id"]:
                flash(t("flash.owner_accounts_can_only_be_edited_by"), "error")
                return redirect(url_for("admin_users"))
            new_name = request.form.get("username", "").strip()
            if not new_name or len(new_name) < 3:
                flash(t("flash.username_must_be_at_least_3_characters_long"), "error")
            elif len(new_name) > 50:
                flash(t("flash.username_cannot_exceed_50_characters"), "error")
            elif not _is_valid_username(new_name):
                flash(
                    t("flash.username_can_only_contain_letters_digits_underscores_and"),
                    "error",
                )
            else:
                existing = db.get_user_by_username(new_name)
                if existing and existing["id"] != user_id:
                    flash(
                        t("flash.username_already_taken_please_choose_another"), "error"
                    )
                else:
                    try:
                        db.update_user(user_id, username=new_name)
                    except db.IntegrityError:
                        flash(
                            t("flash.username_already_taken_please_choose_another"),
                            "error",
                        )
                        return redirect(url_for("admin_users"))
                    else:
                        db.record_username_change(user_id, target["username"], new_name)
                        db.propagate_mention_rename(target["username"], new_name)
                        log_action(
                            "admin_change_username",
                            request,
                            user=current_user,
                            target_user=target["username"],
                            new_username=new_name,
                        )
                        notify_change(
                            "admin_change_username",
                            f"User '{target['username']}' renamed to '{new_name}'",
                        )
                        flash(t("flash.username_updated"), "success")

        elif action == "change_password":
            if target["role"] == "owner" and user_id != current_user["id"]:
                flash(t("flash.owner_accounts_can_only_be_edited_by"), "error")
                return redirect(url_for("admin_users"))
            new_pw = request.form.get("password", "")
            confirm_pw = request.form.get("confirm_password", "")
            if len(new_pw) < MIN_PASSWORD_LENGTH:
                flash(
                    t(
                        "flash.password_must_contain_at_least_minpasswordlength_characters",
                        MIN_PASSWORD_LENGTH=MIN_PASSWORD_LENGTH,
                    ),
                    "error",
                )
            elif len(new_pw) > MAX_PASSWORD_LENGTH:
                flash(
                    t(
                        "flash.password_cannot_exceed_maxpasswordlength_characters",
                        MAX_PASSWORD_LENGTH=MAX_PASSWORD_LENGTH,
                    ),
                    "error",
                )
            elif new_pw != confirm_pw:
                flash(t("flash.passwords_do_not_match"), "error")
            else:
                # Always rotate the target user's session token so any
                # existing sessions on stolen / shared devices are signed
                # out at their next request, regardless of the
                # ``session_limit_enabled`` site setting.  Keep the acting
                # admin's own session alive when self-changing.
                new_token = uuid.uuid4().hex
                update_fields = {
                    "password": generate_password_hash(new_pw),
                    "session_token": new_token,
                }
                if user_id == current_user["id"]:
                    session["session_token"] = new_token
                db.update_user(user_id, **update_fields)
                if user_id == current_user["id"] and current_user_session_id():
                    db.revoke_other_user_sessions(user_id, current_user_session_id())
                else:
                    db.revoke_all_user_sessions(user_id)
                revoked_tokens = _revoke_api_tokens_for_password_reset(
                    user_id, "an admin changed the password"
                )
                log_action(
                    "admin_change_password",
                    request,
                    user=current_user,
                    target_user=target["username"],
                    revoked_api_tokens=revoked_tokens,
                )
                notify_change(
                    "admin_change_password",
                    f"Password changed for '{target['username']}'",
                )
                flash(
                    t("flash.password_updated_other_active_sessions_for_this_user"),
                    "success",
                )

        elif action == "set_temp_password":
            if target["role"] == "owner" and user_id != current_user["id"]:
                flash(t("flash.owner_accounts_can_only_be_edited_by"), "error")
                return redirect(url_for("admin_users"))
            new_pw = request.form.get("password", "")
            if len(new_pw) < MIN_PASSWORD_LENGTH:
                flash(
                    t(
                        "flash.password_must_contain_at_least_minpasswordlength_characters",
                        MIN_PASSWORD_LENGTH=MIN_PASSWORD_LENGTH,
                    ),
                    "error",
                )
            elif len(new_pw) > MAX_PASSWORD_LENGTH:
                flash(
                    t(
                        "flash.password_cannot_exceed_maxpasswordlength_characters",
                        MAX_PASSWORD_LENGTH=MAX_PASSWORD_LENGTH,
                    ),
                    "error",
                )
            else:
                # Backup current password if not already backed up, and
                # always rotate the target's session token (see notes on
                # ``change_password`` above).
                new_token = uuid.uuid4().hex
                update_fields = {
                    "password": generate_password_hash(new_pw),
                    "session_token": new_token,
                }
                if not target["original_password_backup"]:
                    update_fields["original_password_backup"] = target["password"]
                if user_id == current_user["id"]:
                    session["session_token"] = new_token

                db.update_user(user_id, **update_fields)
                if user_id == current_user["id"] and current_user_session_id():
                    db.revoke_other_user_sessions(user_id, current_user_session_id())
                else:
                    db.revoke_all_user_sessions(user_id)
                revoked_tokens = _revoke_api_tokens_for_password_reset(
                    user_id, "an admin set a temporary password"
                )
                log_action(
                    "admin_set_temp_password",
                    request,
                    user=current_user,
                    target_user=target["username"],
                    revoked_api_tokens=revoked_tokens,
                )
                notify_change(
                    "admin_set_temp_password",
                    f"Temporary password set for '{target['username']}'",
                )
                flash(
                    t("flash.temporary_password_set_the_original_password_has_been"),
                    "success",
                )

        elif action == "revert_password":
            if target["role"] == "owner" and user_id != current_user["id"]:
                flash(t("flash.owner_accounts_can_only_be_edited_by"), "error")
                return redirect(url_for("admin_users"))
            if not target["original_password_backup"]:
                flash(
                    t("flash.no_original_password_backup_found_for_this_user"), "error"
                )
            else:
                # Reverting to the original password is still a password
                # change from a security standpoint: rotate the session
                # token unconditionally so the temp-password session is
                # signed out everywhere it was active.
                new_token = uuid.uuid4().hex
                update_fields = {
                    "password": target["original_password_backup"],
                    "original_password_backup": None,
                    "session_token": new_token,
                }
                if user_id == current_user["id"]:
                    session["session_token"] = new_token

                db.update_user(user_id, **update_fields)
                if user_id == current_user["id"] and current_user_session_id():
                    db.revoke_other_user_sessions(user_id, current_user_session_id())
                else:
                    db.revoke_all_user_sessions(user_id)
                revoked_tokens = _revoke_api_tokens_for_password_reset(
                    user_id, "an admin reverted the password"
                )
                log_action(
                    "admin_revert_password",
                    request,
                    user=current_user,
                    target_user=target["username"],
                    revoked_api_tokens=revoked_tokens,
                )
                notify_change(
                    "admin_revert_password",
                    f"Password reverted for '{target['username']}'",
                )
                flash(
                    t("flash.password_has_been_reverted_to_the_original_one"), "success"
                )

        elif action == "userbot_lock":
            if not db.is_plugin_enabled("api_service"):
                flash(t("flash.api_service_disabled"), "error")
                return redirect(url_for("user_profile", username=target["username"]))
            lock_mode = request.form.get("lock_mode", "unlocked")
            if lock_mode not in ("unlocked", "force_enabled", "force_disabled"):
                flash(t("flash.invalid_userbot_lock_mode"), "error")
            else:
                updates = {"userbot_mode_lock": lock_mode}
                if lock_mode == "force_disabled":
                    db.revoke_userbot_tokens(user_id)
                    updates["userbot_enabled"] = 0
                elif lock_mode == "force_enabled":
                    updates["userbot_enabled"] = 1
                    if db.get_userbot_token_info(user_id) is None:
                        db.create_userbot_token(user_id)
                db.update_user(user_id, **updates)
                log_action(
                    "admin_userbot_lock",
                    request,
                    user=current_user,
                    target_user=target["username"],
                    lock_mode=lock_mode,
                )
                flash(
                    t("flash.userbot_lock_mode_has_been_successfully_updated"),
                    "success",
                )

        elif action == "toggle_superuser":
            if not current_user["is_superuser"]:
                flash(t("flash.only_superusers_can_manage_superuser_status"), "error")
            elif target["id"] == current_user["id"]:
                flash(t("flash.you_cannot_change_your_own_superuser_status"), "error")
            else:
                new_state = 0 if target["is_superuser"] else 1
                db.update_user(user_id, is_superuser=new_state)
                log_action(
                    "admin_toggle_superuser",
                    request,
                    user=current_user,
                    target_user=target["username"],
                    enabled=new_state,
                )
                status = "enabled" if new_state else "disabled"
                flash(
                    t(
                        "flash.superuser_status_status_for_username",
                        status=status,
                        username=target["username"],
                    ),
                    "success",
                )

        elif action == "change_role":
            new_role = request.form.get("role", "")
            if user_id == current_user["id"]:
                flash(t("flash.cannot_change_your_own_role"), "error")
            elif (
                new_role == "owner"
                and current_user["role"] != "owner"
                and not current_user.get("is_superuser")
            ):
                flash(t("flash.invalid_role"), "error")
            elif new_role not in ("user", "editor", "admin", "owner"):
                flash(t("flash.invalid_role"), "error")
            elif target["role"] == "owner" and new_role != "owner":
                flash(t("flash.owner_status_can_only_be_changed_by"), "error")
            elif target["role"] == "owner" and db.count_owners() <= 1:
                flash(t("flash.cannot_demote_the_last_owner"), "error")
            elif db.get_role_expiry(user_id) is not None:
                flash(
                    t("flash.this_user_has_a_temporary_role_schedule_remove_a68806"),
                    "error",
                )
            else:
                old_role = target["role"]
                update_fields = {"role": new_role, "custom_role_id": None}
                db.update_user(user_id, **update_fields)
                db.record_role_change(
                    user_id, old_role, new_role, changed_by=current_user["id"]
                )

                # Auto-withdraw pending contributions if user gained direct edit access
                if new_role in ("editor", "admin", "owner") and old_role == "user":
                    withdrawn = db.auto_withdraw_contributions_for_promoted_user(
                        user_id
                    )
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
                    new_role=new_role,
                )
                notify_change(
                    "admin_change_role",
                    f"User '{target['username']}' role changed to '{new_role}'",
                )
                flash(
                    t(
                        "flash.role_updated_user",
                        user="Configure permissions for this user."
                        if new_role in ("editor", "user")
                        else "",
                    ),
                    "success",
                )

        elif action == "suspend":
            if user_id == current_user["id"]:
                flash(t("flash.cannot_suspend_your_own_account"), "error")
            elif target["role"] == "owner":
                flash(t("flash.owner_accounts_cannot_be_suspended_by_other"), "error")
            elif target["role"] == "owner" and db.count_owners() <= 1:
                flash(t("flash.cannot_suspend_the_last_owner"), "error")
            else:
                duration = request.form.get("suspend_duration", "permanent")
                suspended_until = None
                duration_label = "permanent"
                if duration == "custom_datetime":
                    custom_dt = request.form.get("suspend_custom_datetime", "").strip()
                    if custom_dt:
                        try:
                            from helpers._time import get_site_timezone

                            site_tz = get_site_timezone()
                            naive = datetime.strptime(custom_dt, "%Y-%m-%dT%H:%M")
                            local_dt = naive.replace(tzinfo=site_tz)
                            utc_dt = local_dt.astimezone(timezone.utc)
                            if utc_dt > datetime.now(timezone.utc):
                                suspended_until = utc_dt.isoformat()
                                duration_label = f"until {custom_dt}"
                            else:
                                flash(
                                    t("flash.suspension_date_must_be_in_the_future"),
                                    "error",
                                )
                                return redirect(url_for("admin_users"))
                        except (ValueError, TypeError):
                            flash(
                                t(
                                    "flash.invalid_suspension_datetime_please_check_the_format"
                                ),
                                "error",
                            )
                            return redirect(url_for("admin_users"))
                    else:
                        flash(
                            t(
                                "flash.suspension_datetime_is_required_for_custom_date_suspension"
                            ),
                            "error",
                        )
                        return redirect(url_for("admin_users"))
                elif duration == "custom_relative":
                    try:
                        rel_hours = int(
                            request.form.get("suspend_rel_hours", "0") or "0"
                        )
                        rel_minutes = int(
                            request.form.get("suspend_rel_minutes", "0") or "0"
                        )
                        total_minutes = rel_hours * 60 + rel_minutes
                        if total_minutes > 0:
                            from datetime import timedelta

                            suspended_until = (
                                datetime.now(timezone.utc)
                                + timedelta(minutes=total_minutes)
                            ).isoformat()
                            duration_label = f"{rel_hours}h {rel_minutes}m"
                        else:
                            flash(
                                t(
                                    "flash.suspension_duration_must_be_greater_than_zero"
                                ),
                                "error",
                            )
                            return redirect(url_for("admin_users"))
                    except (ValueError, TypeError):
                        flash(
                            t(
                                "flash.invalid_suspension_duration_please_enter_valid_numbers"
                            ),
                            "error",
                        )
                        return redirect(url_for("admin_users"))
                elif duration and duration != "permanent":
                    try:
                        hours = int(duration)
                        if hours > 0:
                            from datetime import timedelta

                            suspended_until = (
                                datetime.now(timezone.utc) + timedelta(hours=hours)
                            ).isoformat()
                            duration_label = f"{hours} hours"
                    except (ValueError, TypeError):
                        pass

                reason = request.form.get("suspend_reason", "").strip()[
                    :MAX_SUSPEND_REASON_LENGTH
                ]
                reason_visible = request.form.get(
                    "suspend_reason_visible"
                ) == "1" and bool(reason)
                time_visible = (
                    request.form.get("suspend_time_visible") == "1"
                    and suspended_until is not None
                )

                db.update_user(
                    user_id,
                    suspended=1,
                    suspended_until=suspended_until,
                    suspend_reason=reason or None,
                    suspend_reason_visible=int(reason_visible),
                    suspend_time_visible=int(time_visible),
                )
                db.record_suspension_action(
                    user_id,
                    "suspend",
                    performed_by=current_user["id"],
                    reason=reason or None,
                    reason_visible=reason_visible,
                    time_visible=time_visible,
                    duration=duration_label,
                    suspended_until=suspended_until,
                )
                log_action(
                    "admin_suspend",
                    request,
                    user=current_user,
                    target_user=target["username"],
                )
                if suspended_until:
                    from helpers._time import format_datetime

                    expiry_str = format_datetime(suspended_until)
                    notify_change(
                        "admin_suspend",
                        f"User '{target['username']}' suspended until {expiry_str}",
                    )
                    flash(
                        t(
                            "flash.user_suspended_until_expirystr",
                            expiry_str=expiry_str,
                        ),
                        "success",
                    )
                else:
                    notify_change(
                        "admin_suspend",
                        f"User '{target['username']}' suspended permanently",
                    )
                    flash(t("flash.user_suspended_permanently"), "success")

        elif action == "unsuspend":
            db.update_user(
                user_id,
                suspended=0,
                suspended_until=None,
                suspend_reason=None,
                suspend_reason_visible=0,
                suspend_time_visible=0,
            )
            db.record_suspension_action(
                user_id,
                "unsuspend",
                performed_by=current_user["id"],
            )
            log_action(
                "admin_unsuspend",
                request,
                user=current_user,
                target_user=target["username"],
            )
            notify_change("admin_unsuspend", f"User '{target['username']}' unsuspended")
            flash(t("flash.user_unsuspended"), "success")

        elif action == "delete":
            if user_id == current_user["id"]:
                flash(t("flash.cannot_delete_your_own_account_from_here_use"), "error")
            elif target["role"] == "owner":
                flash(t("flash.owner_accounts_cannot_be_deleted_by_other"), "error")
            elif target["role"] == "owner" and db.count_owners() <= 1:
                flash(t("flash.cannot_delete_the_last_owner"), "error")
            elif db.get_user_expiry(user_id) is not None:
                flash(
                    t("flash.this_user_is_scheduled_for_automatic_deletion_remove"),
                    "error",
                )
            elif db.get_role_expiry(user_id) is not None:
                flash(
                    t("flash.this_user_has_a_temporary_role_schedule_remove_adea19"),
                    "error",
                )
            else:
                db.propagate_mention_deletion(target["username"])
                admin_del_profile = db.get_user_profile(user_id)
                try:
                    db.delete_user_field_values(user_id)
                except Exception:
                    pass
                db.delete_user(user_id)
                log_action(
                    "admin_delete_user",
                    request,
                    user=current_user,
                    target_user=target["username"],
                )
                notify_change(
                    "admin_delete_user", f"User '{target['username']}' deleted"
                )
                if admin_del_profile and admin_del_profile["avatar_filename"]:
                    safe_unlink_in(
                        config.UPLOAD_FOLDER, admin_del_profile["avatar_filename"]
                    )
                    notify_file_deleted(admin_del_profile["avatar_filename"])
                flash(t("flash.user_deleted"), "success")

        elif action == "approve_user":
            if target["approval_status"] != "pending":
                flash(t("flash.user_is_not_pending_approval"), "error")
            else:
                db.approve_user(user_id, current_user["id"])
                log_action(
                    "admin_approve_user",
                    request,
                    user=current_user,
                    target_user=target["username"],
                )
                notify_change(
                    "admin_approve_user", f"User '{target['username']}' approved"
                )
                flash(t("flash.user_has_been_approved"), "success")

        elif action == "deny_user":
            if target["approval_status"] != "pending":
                flash(t("flash.user_is_not_pending_approval"), "error")
            else:
                db.deny_user(user_id, current_user["id"])
                log_action(
                    "admin_deny_user",
                    request,
                    user=current_user,
                    target_user=target["username"],
                )
                notify_change("admin_deny_user", f"User '{target['username']}' denied")
                flash(t("flash.user_has_been_denied"), "success")

        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<string:user_id>/editor-access", methods=["GET", "POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_editor_access(user_id):
        """Deprecated editor access UI."""
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)
        flash(
            t("flash.peruser_category_access_customization_has_been_deprecated_us"),
            "info",
        )
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<string:user_id>/permissions", methods=["GET", "POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_user_permissions(user_id):
        """Deprecated per-user permission UI."""
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)
        flash(
            t("flash.peruser_permission_customization_has_been_deprecated_use_cus"),
            "info",
        )
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/create", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_create_user():
        """Admin: create a new user account with the specified username, password, and role."""
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        role = request.form.get("role", "user")
        force_password_change = 1 if request.form.get("force_password_change") else 0
        current_user = get_current_user()

        if not username or not password:
            flash(t("flash.username_and_password_are_required"), "error")
        elif len(username) < 3:
            flash(t("flash.username_must_be_at_least_3_characters_long"), "error")
        elif len(username) > 50:
            flash(t("flash.username_cannot_exceed_50_characters"), "error")
        elif not _is_valid_username(username):
            flash(
                t("flash.username_can_only_contain_letters_digits_underscores_and"),
                "error",
            )
        elif password != confirm:
            flash(t("flash.passwords_do_not_match"), "error")
        elif len(password) < MIN_PASSWORD_LENGTH:
            flash(
                t(
                    "flash.password_must_contain_at_least_minpasswordlength_characters",
                    MIN_PASSWORD_LENGTH=MIN_PASSWORD_LENGTH,
                ),
                "error",
            )
        elif len(password) > MAX_PASSWORD_LENGTH:
            flash(
                t(
                    "flash.password_cannot_exceed_maxpasswordlength_characters",
                    MAX_PASSWORD_LENGTH=MAX_PASSWORD_LENGTH,
                ),
                "error",
            )
        elif role not in ("user", "editor", "admin"):
            flash(t("flash.invalid_role"), "error")
        elif db.get_user_by_username(username):
            flash(t("flash.username_already_taken_please_choose_another"), "error")
        else:
            hashed = generate_password_hash(password)
            try:
                user_id = db.create_user(username, hashed, role=role)
                updates = {}
                if db.get_site_settings().get("new_user_intro_enabled"):
                    updates["intro_required"] = 1
                if force_password_change:
                    updates["force_password_change"] = 1
                if updates:
                    db.update_user(user_id, **updates)
            except db.IntegrityError:
                flash(t("flash.username_already_taken_please_choose_another"), "error")
                return redirect(url_for("admin_users"))

            log_action(
                "admin_create_user",
                request,
                user=current_user,
                new_username=username,
                role=role,
            )
            notify_change(
                "admin_create_user", f"User '{username}' created with role '{role}'"
            )
            emit_hook("after_user_create", user=db.get_user_by_id(user_id))
            flash(t("flash.user_username_created", username=username), "success")

        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<string:user_id>/audit")
    @login_required
    @admin_required
    def admin_user_audit(user_id):
        """Admin: view the activity audit log and username history for a specific user."""
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)
        log_entries = _read_user_audit_log(target["username"])
        username_history = db.get_username_history(user_id)
        suspension_history = db.get_suspension_history(user_id)
        return render_template(
            "admin/audit.html",
            target=target,
            log_entries=log_entries,
            username_history=username_history,
            suspension_history=suspension_history,
        )

    @app.route("/admin/users/<string:user_id>/export")
    @login_required
    @admin_required
    @rate_limit(10, 60, exempt_html_nav=False)
    def admin_export_user_data(user_id):
        """Allow an admin to download all data for any user as a ZIP file."""
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)
        current_user = get_current_user()
        if target["is_superuser"]:
            flash(t("flash.this_account_is_protected_and_cannot_be_modified"), "error")
            return redirect(url_for("admin_users"))
        if target["role"] == "owner" and user_id != current_user["id"]:
            flash(t("flash.owner_accounts_can_only_be_edited_by"), "error")
            return redirect(url_for("admin_users"))
        buf = build_user_export_zip(target)
        log_action(
            "admin_export_user_data",
            request,
            user=current_user,
            target_user=target["username"],
        )
        filename = f"userdata_{target['username']}.zip"
        return send_file(
            buf, mimetype="application/zip", as_attachment=True, download_name=filename
        )

    @app.route("/admin/users/<string:user_id>/toggle_chat", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_toggle_chat(user_id):
        """Enable or disable chat access for a user (Legacy)."""
        target = db.get_user_by_id(user_id)
        if not target:
            flash(t("flash.user_not_found"), "error")
            return redirect(url_for("admin_users"))
        admin = get_current_user()
        if target["is_superuser"]:
            flash(t("flash.this_account_is_protected_and_cannot_be_modified"), "error")
            return redirect(url_for("admin_users"))
        if target["role"] == "owner" and user_id != admin["id"]:
            flash(t("flash.owner_accounts_can_only_be_edited_by"), "error")
            return redirect(url_for("admin_users"))
        currently_disabled = db.is_user_chat_disabled(user_id)
        db.set_user_chat_disabled(user_id, not currently_disabled)
        action = "disabled" if not currently_disabled else "enabled"
        log_action(
            "admin_toggle_chat",
            request,
            user=admin,
            target_user=target["username"],
            chat_status=action,
        )
        if not currently_disabled:
            flash(t("flash.your_chat_privileges_have_been_disabled_by_an"), "error")
        flash(
            t(
                "flash.chat_access_action_for_username",
                action=action,
                username=target["username"],
            ),
            "success",
        )
        return redirect(url_for("admin_users"))

    @app.route("/admin/merge-requests", methods=["GET"])
    @login_required
    @admin_required
    def admin_merge_requests():
        """List all merge requests (both pending and completed)."""
        status_filter = request.args.get("status", "pending")
        status_in = (
            ("pending", "approved_by_both")
            if status_filter == "pending"
            else ("pending", "approved_by_both", "merged", "cancelled", "denied")
        )
        rows = (
            db.get_db()
            .execute(
                """SELECT * FROM account_merge_requests
                WHERE status IN (%s)
                ORDER BY created_at DESC"""
                % ",".join(["?"] * len(status_in)),
                status_in,
            )
            .fetchall()
        )

        enriched = []
        for req in rows:
            source = db.get_user_by_id(req["source_user_id"])
            target = db.get_user_by_id(req["target_user_id"])
            creator = db.get_user_by_id(req["created_by"])
            enriched.append(
                {
                    **req,
                    "source_username": source["username"] if source else "deleted",
                    "target_username": target["username"] if target else "deleted",
                    "creator_username": creator["username"] if creator else "deleted",
                    "source_ready": req["source_approved"],
                    "target_ready": req["target_approved"],
                    "both_ready": req.get("source_approved")
                    and req.get("target_approved"),
                }
            )

        return render_template(
            "admin/merge_requests.html",
            requests=enriched,
            status_filter=status_filter,
        )

    @app.route("/admin/merge-requests/<int:merge_id>/approve", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_approve_merge_request(merge_id):
        """Admin approves/forces a merge request, bypassing user approvals."""
        try:
            req = db.approve_by_admin(merge_id, get_current_user()["id"])
            source = db.get_user_by_id(req["source_user_id"])
            target = db.get_user_by_id(req["target_user_id"])
            log_action(
                "admin_approve_merge_request",
                request,
                user=get_current_user(),
                merge_id=merge_id,
                source=source["username"] if source else "deleted",
                target=target["username"] if target else "deleted",
            )
            flash(
                t("flash.merge_request_approved_by_admin"),
                "success",
            )
        except ValueError as exc:
            flash(str(exc), "error")

        return redirect(url_for("admin_merge_requests"))

    @app.route("/admin/users/merge", methods=["GET", "POST"])
    @login_required
    @admin_required
    def admin_account_merge():
        """Admin-initiated account merge: two users can be merged directly."""
        if request.method == "POST":
            source_username = (request.form.get("source_username", "")).strip()
            target_username = (request.form.get("target_username", "")).strip()
            action = request.form.get("action", "")
            delete_source = request.form.get("delete_source") == "1"

            if not source_username or not target_username:
                flash(t("flash.both_usernames_are_required"), "error")
                return render_template("admin/account_merge.html")

            source = db.get_user_by_username(source_username)
            target = db.get_user_by_username(target_username)

            if not source:
                flash(
                    t(
                        "flash.no_user_found_with_source_username",
                        source_username=source_username,
                    ),
                    "error",
                )
                return render_template("admin/account_merge.html")
            if not target:
                flash(
                    t(
                        "flash.no_user_found_with_target_username",
                        target_username=target_username,
                    ),
                    "error",
                )
                return render_template("admin/account_merge.html")
            if source["id"] == target["id"]:
                flash(t("flash.cannot_merge_account_with_itself"), "error")
                return render_template("admin/account_merge.html")

            # Prevent merging superusers
            if source["is_superuser"] or target["is_superuser"]:
                flash(t("flash.cannot_merge_superuser_accounts"), "error")
                return render_template("admin/account_merge.html")

            admin = get_current_user()

            if action == "preview":
                # Calculate summary of data to be transferred
                conn = db.get_db()
                page_count = conn.execute(
                    "SELECT COUNT(*) as c FROM page_history WHERE edited_by = ?",
                    (source["id"],),
                ).fetchone()["c"]
                draft_count = conn.execute(
                    "SELECT COUNT(*) as c FROM drafts WHERE user_id = ?",
                    (source["id"],),
                ).fetchone()["c"]
                canvas_count = conn.execute(
                    "SELECT COUNT(*) as c FROM canvas__layouts WHERE creator_id = ?",
                    (source["id"],),
                ).fetchone()["c"]
                kanban_count = conn.execute(
                    "SELECT COUNT(*) as c FROM kanban_boards WHERE created_by = ?",
                    (source["id"],),
                ).fetchone()["c"]
                badges_count = conn.execute(
                    "SELECT COUNT(*) as c FROM user_badges WHERE user_id = ? AND revoked = 0",
                    (source["id"],),
                ).fetchone()["c"]
                member_count = conn.execute(
                    "SELECT COUNT(*) as c FROM group_members WHERE user_id = ?",
                    (source["id"],),
                ).fetchone()["c"]
                attempts_count = conn.execute(
                    "SELECT COUNT(*) as c FROM assessment_attempts WHERE user_id = ?",
                    (source["id"],),
                ).fetchone()["c"]
                return render_template(
                    "admin/account_merge.html",
                    source_user=source,
                    target_user=target,
                    preview=True,
                    preview_data={
                        "page_revisions": page_count,
                        "drafts": draft_count,
                        "canvas_layouts": canvas_count,
                        "kanban_boards": kanban_count,
                        "badges": badges_count,
                        "group_memberships": member_count,
                        "assessment_attempts": attempts_count,
                    },
                    delete_source=delete_source,
                )

            if action == "execute":
                try:
                    result = db.admin_execute_merge(
                        source["id"],
                        target["id"],
                        admin["id"],
                        lock_source=not delete_source,
                    )
                    # Update the source_user's username to avoid confusion
                    if result.get("source_locked"):
                        db.update_user(
                            source["id"], username=f"merged_{source['username']}"
                        )
                    log_action(
                        "admin_merge_accounts",
                        request,
                        user=admin,
                        source=source["username"],
                        target=target["username"],
                        source_locked=result.get("source_locked", True),
                    )
                    notify_change(
                        "admin_merge_accounts",
                        f"Admin '{admin['username']}' merged account '{source['username']}' into '{target['username']}'",
                    )
                    flash(
                        t(
                            "flash.accounts_merged_successfully",
                            target=target["username"],
                        ),
                        "success",
                    )
                except ValueError as exc:
                    flash(str(exc), "error")

                return redirect(url_for("admin_merge_requests"))

        return render_template("admin/account_merge.html")

    @app.route("/admin/requests/<int:merge_id>/deny", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_deny_merge_request(merge_id):
        """Admin denies a pending merge request."""
        try:
            db.deny_merge(merge_id)
            log_action(
                "admin_deny_merge_request",
                request,
                user=get_current_user(),
                merge_id=merge_id,
            )
            flash(t("flash.merge_request_denied"), "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_merge_requests"))

    @app.route("/admin/requests/<int:merge_id>/cancel", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_cancel_merge_request(merge_id):
        """Admin cancels a pending merge request."""
        try:
            db.cancel_merge(merge_id, get_current_user()["id"])
            log_action(
                "admin_cancel_merge_request",
                request,
                user=get_current_user(),
                merge_id=merge_id,
            )
            flash(t("flash.merge_request_cancelled"), "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_merge_requests"))
