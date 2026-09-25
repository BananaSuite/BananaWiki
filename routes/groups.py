"""
BananaWiki: Group chat routes.
"""

import os
import re
import uuid
import json
import zipfile
import io
import hashlib
from datetime import datetime, timezone, timedelta
from werkzeug.utils import secure_filename

from flask import (
    render_template, request, redirect, url_for, flash,
    send_file, send_from_directory, abort, jsonify,
)

import db
import config
from helpers import (
    login_required, admin_required, get_current_user, rate_limit,
    get_effective_chat_cleanup_settings, is_joke_audio_extension, get_real_audio_ext,
    get_joke_fail_message, convert_joke_audio,
    t,
)
from wiki_logger import log_action
from sync import notify_change
from routes.chat import attachment_download_mimetype

DELETED_MESSAGE_PLACEHOLDER = t("chat.deleted_message")


def _chat_allowed_file(filename, settings=None):
    """Delegate to the shared ``allowed_chat_file`` helper."""
    from helpers._validation import allowed_chat_file
    return allowed_chat_file(filename, settings=settings)


def register_group_routes(app):
    """Register group chat routes on the Flask app."""

    def _chat_disabled_redirect(endpoint="home", **values):
        """Redirect users whose chat access has been disabled."""
        flash(t("flash.your_chat_privileges_have_been_disabled_by_an"), "error")
        return redirect(url_for(endpoint, **values))

    def _group_message_permissions(group, user):
        """Return moderation flags for group message controls."""
        my_role = db.get_group_member_role(group["id"], user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        can_delete_any_message = is_site_admin or my_role in ("owner", "moderator")
        return my_role, is_site_admin, can_delete_any_message

    def _group_message_state(group, user):
        """Return rendered group message HTML and lightweight counters for live refresh."""
        _, is_site_admin, can_delete_any_message = _group_message_permissions(group, user)
        messages = db.get_group_messages(group["id"])
        attachment_count = sum(len(msg.get("attachments") or []) for msg in messages)
        rendered_messages = _prepare_group_messages(messages, include_deleted_content=is_site_admin)
        latest_message_id = rendered_messages[-1]["id"] if rendered_messages else 0
        html = render_template(
            "groups/_messages.html",
            group=group,
            messages=rendered_messages,
            can_delete_any_message=can_delete_any_message,
            is_site_admin=is_site_admin,
        )
        return rendered_messages, {
            "html": html,
            "message_count": len(rendered_messages),
            "attachment_count": attachment_count,
            "latest_message_id": latest_message_id,
            "state_token": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        }, can_delete_any_message

    def _prepare_group_messages(messages, include_deleted_content=False):
        """Return group messages annotated for display.

        When ``include_deleted_content`` is False, deleted non-system messages
        are replaced with a placeholder and their attachments are hidden.
        When True, the original deleted content and attachments remain visible
        for site-admin monitoring views.
        """
        prepared_messages = []
        for msg in messages:
            prepared = dict(msg)
            if prepared.get("is_system"):
                prepared["display_content"] = prepared.get("content", "")
                prepared["display_attachments"] = list(prepared.get("attachments") or [])
                prepared["show_deleted_original"] = False
                prepared_messages.append(prepared)
                continue
            is_deleted = bool(prepared.get("is_deleted"))
            prepared["show_deleted_original"] = is_deleted and include_deleted_content
            prepared["display_content"] = (
                prepared.get("content", "")
                if not is_deleted or include_deleted_content
                else DELETED_MESSAGE_PLACEHOLDER
            )
            prepared["display_attachments"] = (
                list(prepared.get("attachments") or [])
                if not is_deleted or include_deleted_content
                else []
            )
            prepared_messages.append(prepared)
        return prepared_messages

    @app.route("/groups")
    @login_required
    def group_list():
        """List all group chats the current user belongs to."""
        user = get_current_user()
        if not db.has_permission(user, "chat.group"):
            return _chat_disabled_redirect()
        settings = db.get_site_settings()
        if settings and not settings["chat_group_enabled"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.group_chats_are_currently_disabled"), "error")
            return redirect(url_for("home"))
        groups = db.get_user_groups(user["id"])
        total_unread_group = db.get_total_unread_group_count(user["id"])
        return render_template("groups/list.html", groups=groups,
                               total_unread_group=total_unread_group)

    @app.route("/groups/new", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 60)
    def group_new():
        """Create a new group chat."""
        user = get_current_user()
        if not db.has_permission(user, "chat.group"):
            return _chat_disabled_redirect()
        if not db.has_permission(user, "chat.create_group"):
            flash(t("flash.you_do_not_have_permission_to_create_group"), "error")
            return redirect(url_for("group_list"))
        settings = db.get_site_settings()
        if settings and not settings["chat_group_enabled"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.group_chats_are_currently_disabled"), "error")
            return redirect(url_for("home"))
        if settings and not settings["chat_allow_group_creation"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.creating_new_group_chats_is_currently_disabled"), "error")
            return redirect(url_for("group_list"))
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            if not name:
                flash(t("flash.group_name_is_required"), "error")
                return redirect(url_for("group_new"))
            if len(name) > 100:
                flash(t("flash.group_name_too_long_100_character_limit"), "error")
                return redirect(url_for("group_new"))
            if len(description) > 500:
                flash(t("flash.group_description_cannot_exceed_500_characters"), "error")
                return redirect(url_for("group_new"))
            group = db.create_group_chat(name, user["id"], description)
            db.send_group_system_message(group["id"], f"{user['username']} created the group")
            notify_change("group_create", f"Group '{name}' created by {user['username']}")
            flash(t("flash.group_has_been_successfully_created"), "success")
            return redirect(url_for("group_view", group_id=group["id"]))
        return render_template("groups/new.html")

    @app.route("/groups/join", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 60)
    def group_join():
        """Join an existing group chat using an invite code."""
        user = get_current_user()
        if not db.has_permission(user, "chat.group"):
            return _chat_disabled_redirect()
        settings = db.get_site_settings()
        if settings and not settings["chat_group_enabled"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.group_chats_are_currently_disabled"), "error")
            return redirect(url_for("home"))
        if request.method == "POST":
            code = request.form.get("invite_code", "").strip()
            if not code:
                flash(t("flash.please_enter_an_invite_code"), "error")
                return redirect(url_for("group_join"))
            group = db.get_group_chat_by_invite(code)
            if not group:
                flash(t("flash.invalid_invite_code"), "error")
                return redirect(url_for("group_join"))
            if db.is_group_member_banned(group["id"], user["id"]):
                flash(t("flash.you_are_currently_banned_from_this_group"), "error")
                return redirect(url_for("group_join"))
            if db.is_group_member(group["id"], user["id"]):
                flash(t("flash.you_are_already_a_member_of_this_group"), "info")
                return redirect(url_for("group_view", group_id=group["id"]))
            db.add_group_member(group["id"], user["id"])
            db.send_group_system_message(group["id"], f"{user['username']} joined the group")
            notify_change("group_join", f"{user['username']} joined group '{group['name']}'")
            flash(t("flash.you_joined_name", name=group['name']), "success")
            return redirect(url_for("group_view", group_id=group["id"]))
        return render_template("groups/join.html")

    @app.route("/groups/global")
    @login_required
    def group_global():
        """Join the global chat (auto-join) and redirect to it."""
        user = get_current_user()
        if not db.has_permission(user, "chat.group"):
            return _chat_disabled_redirect()
        settings = db.get_site_settings()
        if settings and not settings["chat_group_enabled"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.group_chats_are_currently_disabled"), "error")
            return redirect(url_for("home"))
        group = db.get_or_create_global_chat()
        if db.is_group_member_banned(group["id"], user["id"]):
            flash(t("flash.you_are_currently_banned_from_this_group"), "error")
            return redirect(url_for("group_list"))
        if not db.is_group_member(group["id"], user["id"]):
            db.add_group_member(group["id"], user["id"])
            db.send_group_system_message(group["id"], f"{user['username']} joined the group")
        return redirect(url_for("group_view", group_id=group["id"]))

    @app.route("/groups/<int:group_id>")
    @login_required
    def group_view(group_id):
        """View messages and member list for a group chat."""
        user = get_current_user()
        if not db.has_permission(user, "chat.group"):
            return _chat_disabled_redirect()
        settings = db.get_site_settings()
        if settings and not settings["chat_group_enabled"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.group_chats_are_currently_disabled"), "error")
            return redirect(url_for("home"))
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        # is_group_member() is False for banned members, so a ban also ends
        # read access to the chat, its attachments and its invite code.
        is_group_member = db.is_group_member(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        if not is_group_member and not is_site_admin:
            if db.is_group_member_banned(group_id, user["id"]):
                flash(t("flash.you_are_currently_banned_from_this_group"), "error")
            else:
                flash(t("flash.you_are_not_a_member_of_this_group"), "error")
            return redirect(url_for("group_list"))
        # Reset unread count when viewing the group
        if is_group_member:
            db.reset_group_unread_count(group_id, user["id"])
        my_role, is_site_admin, can_delete_any_message = _group_message_permissions(group, user)
        messages, message_state, _ = _group_message_state(group, user)
        members = db.get_group_members(group_id)
        banned_members = db.get_group_banned_members(group_id)
        my_membership = db.get_group_member(group_id, user["id"])
        all_users = db.list_users()
        cleanup_settings = get_effective_chat_cleanup_settings(settings)
        return render_template("groups/chat.html", group=group, messages=messages,
                               members=members, banned_members=banned_members,
                               my_membership=my_membership, all_users=all_users,
                               message_state=message_state, can_delete_any_message=can_delete_any_message,
                               is_site_admin=is_site_admin, is_group_member=is_group_member, my_role=my_role,
                                show_cleanup_banner=(
                                    settings and settings["chat_cleanup_enabled"]
                                    and (
                                       cleanup_settings["group"]["auto_clear_messages"]
                                       or cleanup_settings["group"]["auto_clear_attachments"]
                                   )
                               ),
                               clear_attachment_count=message_state["attachment_count"])

    @app.route("/groups/<int:group_id>/messages")
    @login_required
    @rate_limit(60, 60)
    def group_messages_partial(group_id):
        """Return the latest group chat HTML for live refresh."""
        user = get_current_user()
        if not db.has_permission(user, "chat.group"):
            return jsonify({"error": "You do not have permission to use group chats."}), 403
        settings = db.get_site_settings()
        if settings and not settings["chat_group_enabled"] and user["role"] not in ("admin", "owner"):
            return jsonify({"error": "Group chats are currently disabled."}), 403
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        is_group_member = db.is_group_member(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        if not is_group_member and not is_site_admin:
            return jsonify({"error": "You are not a member of this group."}), 403
        if is_group_member:
            db.reset_group_unread_count(group_id, user["id"])
        _, message_state, _ = _group_message_state(group, user)
        return jsonify(message_state)

    @app.route("/groups/<int:group_id>/send", methods=["POST"])
    @login_required
    @rate_limit(30, 60)
    def group_send(group_id):
        """Send a message (and optional file attachment) to a group chat."""
        user = get_current_user()
        if not db.has_permission(user, "chat.group"):
            return _chat_disabled_redirect()
        settings = db.get_site_settings()
        if settings and not settings["chat_group_enabled"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.group_chats_are_currently_disabled"), "error")
            return redirect(url_for("home"))
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        if group["is_global"] and not group["is_active"]:
            flash(t("flash.the_global_chat_is_currently_deactivated"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        # The ban check comes first: is_group_member() is also False for a
        # banned member, and the ban message is the more useful one.
        if db.is_group_member_banned(group_id, user["id"]):
            flash(t("flash.you_are_currently_banned_from_this_group"), "error")
            return redirect(url_for("group_list"))
        if not db.is_group_member(group_id, user["id"]):
            flash(t("flash.access_denied"), "error")
            return redirect(url_for("group_list"))
        if db.is_group_member_timed_out(group_id, user["id"]):
            flash(t("flash.you_are_timed_out_and_cannot_send_messages"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        content = request.form.get("content", "").strip()
        if not content:
            flash(t("flash.message_cannot_be_empty"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        max_msg_len = settings["chat_max_message_length"] if settings and settings["chat_max_message_length"] else 5000
        if len(content) > max_msg_len:
            flash(t("flash.message_cannot_exceed_maxmsglen_characters", max_msg_len=max_msg_len), "error")
            return redirect(url_for("group_view", group_id=group_id))

        # Validate and write file attachment *before* committing the message
        # so that a rejected attachment never leaves a phantom text message.
        attachment_info = None
        if "attachment" in request.files:
            f = request.files["attachment"]
            if f.filename:
                # Check if attachments are enabled
                if settings and not settings["chat_attachments_enabled"] and user["role"] not in ("admin", "owner"):
                    flash(t("flash.file_attachments_are_currently_disabled"), "error")
                    return redirect(url_for("group_view", group_id=group_id))
                # Check daily limit from site settings
                max_attachments = settings["chat_attachments_per_day_limit"] if settings and settings["chat_attachments_per_day_limit"] else 10
                att_count = db.get_user_group_attachment_count_today(user["id"])
                if att_count >= max_attachments:
                    flash(t("flash.daily_attachment_limit_of_maxattachments_files_reached", max_attachments=max_attachments), "error")
                    return redirect(url_for("group_view", group_id=group_id))
                if not _chat_allowed_file(f.filename, settings=settings):
                    flash(t("flash.file_type_not_allowed"), "error")
                    return redirect(url_for("group_view", group_id=group_id))
                original_name = secure_filename(f.filename) or "file"
                ext = original_name.rsplit(".", 1)[1].lower() if "." in original_name else "bin"
                is_joke = is_joke_audio_extension(ext)
                stored_ext = get_real_audio_ext(ext) if is_joke else ext
                stored_name = f"{uuid.uuid4().hex}.{stored_ext}"
                os.makedirs(config.CHAT_ATTACHMENT_FOLDER, exist_ok=True)
                filepath = os.path.join(config.CHAT_ATTACHMENT_FOLDER, stored_name)
                # Chunked write with size enforcement (limit from site settings, in bytes)
                max_att_size_mb = settings["chat_max_attachment_size_mb"] if settings and settings["chat_max_attachment_size_mb"] else 5
                max_att_size = max_att_size_mb * 1024 * 1024
                file_size = 0
                chunk_size = 64 * 1024
                oversized = False
                _blob_buf = io.BytesIO()  # accumulate content for DB blob (dual-write)
                with open(filepath, "wb") as out:
                    while True:
                        chunk = f.stream.read(chunk_size)
                        if not chunk:
                            break
                        file_size += len(chunk)
                        if file_size > max_att_size:
                            oversized = True
                            break
                        out.write(chunk)
                        _blob_buf.write(chunk)
                if oversized:
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
                    flash(t("flash.file_exceeds_the_maxattsizemb_mb_limit", max_att_size_mb=max_att_size_mb), "error")
                    return redirect(url_for("group_view", group_id=group_id))
                # Joke audio: convert mp5/mp7 → mp3 via ffmpeg
                if is_joke:
                    ok, err = convert_joke_audio(filepath, filepath)
                    if not ok:
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        flash(f"{get_joke_fail_message(ext)} {err or ''}", "error")
                        return redirect(url_for("group_view", group_id=group_id))
                attachment_info = (stored_name, original_name, file_size)
                # Dual-write: store buffered content in DB blob
                try:
                    blob_data = _blob_buf.getvalue()
                    if is_joke:
                        try:
                            with open(filepath, "rb") as cf:
                                blob_data = cf.read()
                        except OSError:
                            pass
                    _blob_id = db.store_blob(stored_name, blob_data, "application/octet-stream")
                    attachment_info = (stored_name, original_name, file_size, _blob_id)
                except Exception:
                    pass  # blob storage is non-critical

        ip_address = request.remote_addr or "unknown"
        msg_id = db.send_group_message(group_id, user["id"], content, ip_address)

        # Increment unread count for all other members in a single batch query
        db.bulk_increment_group_unread_count(group_id, user["id"])

        if attachment_info:
            db.add_group_attachment(msg_id, *attachment_info)

        log_action("group_chat_send", request, user=user, group_id=group_id)
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/attachments/<int:attachment_id>/download")
    @login_required
    def group_attachment_download(attachment_id):
        """Download a file attachment from a group chat message."""
        att = db.get_group_attachment(attachment_id)
        if not att:
            abort(404)
        user = get_current_user()
        if not db.has_permission(user, "chat.group"):
            abort(403)
        is_admin = user["role"] in ("admin", "owner")
        if att["is_deleted"] and not is_admin:
            abort(403)
        if not is_admin and not db.is_group_member(att["group_id"], user["id"]):
            abort(403)
        att_dir = os.path.abspath(config.CHAT_ATTACHMENT_FOLDER)
        # Try DB blob first, then fall back to disk
        blob_id = att.get("blob_id")  # group attachment is returned as dict
        mimetype = attachment_download_mimetype(att["original_name"])
        if blob_id:
            content = db.get_blob_content(blob_id)
            if content:
                return send_file(
                    io.BytesIO(content),
                    as_attachment=True,
                    download_name=att["original_name"],
                    mimetype=mimetype,
                )
        filepath = os.path.abspath(os.path.join(att_dir, att["filename"]))
        if os.path.commonpath([att_dir, filepath]) != att_dir:
            abort(400)
        if not os.path.isfile(filepath):
            abort(404)
        return send_from_directory(att_dir, att["filename"],
                                   as_attachment=True,
                                   download_name=att["original_name"],
                                   mimetype=mimetype)

    @app.route("/groups/<int:group_id>/members/add", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def group_add_member(group_id):
        """Add a user to a group chat (owner, moderator, or site admin only)."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        if my_role not in ("owner", "moderator") and not is_site_admin:
            flash(t("flash.you_do_not_have_permission_to_add_members"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        username = request.form.get("username", "").strip()
        if not username:
            flash(t("flash.please_enter_a_username"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target = db.get_user_by_username(username)
        if not target:
            flash(t("flash.the_specified_user_was_not_found"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        if db.is_group_member_banned(group_id, target["id"]):
            flash(t("flash.user_is_banned_from_this_group_revoke_the"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        if db.is_group_member(group_id, target["id"]):
            flash(t("flash.user_is_already_a_member"), "info")
            return redirect(url_for("group_view", group_id=group_id))
        db.add_group_member(group_id, target["id"])
        db.send_group_system_message(group_id, f"{target['username']} was added by {user['username']}")
        notify_change("group_add_member", f"{target['username']} added to group '{group['name']}' by {user['username']}")
        log_action("group_add_member", request, user=user, group_id=group_id, target_user_id=target["id"])
        flash(t("flash.username_has_been_added_to_the_group", username=target['username']), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/leave", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def group_leave(group_id):
        """Leave a group chat (owners must transfer ownership first)."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role = db.get_group_member_role(group_id, user["id"])
        if not my_role:
            flash(t("flash.you_are_not_a_member_of_this_group"), "error")
            return redirect(url_for("group_list"))
        if my_role == "owner" and not group["is_global"]:
            flash(t("flash.you_must_transfer_ownership_before_leaving"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        db.remove_group_member(group_id, user["id"])
        db.send_group_system_message(group_id, f"{user['username']} left the group")
        notify_change("group_leave", f"{user['username']} left group '{group['name']}'")
        flash(t("flash.you_left_the_group"), "success")
        return redirect(url_for("group_list"))

    @app.route("/groups/<int:group_id>/kick", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def group_kick(group_id):
        """Kick (remove) a member from a group chat (owner/moderator or site admin only)."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        if group["is_global"]:
            if not is_site_admin:
                flash(t("flash.permission_denied"), "error")
                return redirect(url_for("group_view", group_id=group_id))
        else:
            if my_role not in ("owner", "moderator") and not is_site_admin:
                flash(t("flash.permission_denied"), "error")
                return redirect(url_for("group_view", group_id=group_id))
        target_id = request.form.get("user_id", "")
        target_membership = db.get_group_member(group_id, target_id)
        if not target_membership:
            flash(t("flash.user_is_not_a_member_of_the_group"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        # Moderators cannot kick other moderators or the owner
        if not is_site_admin and my_role == "moderator" and target_membership["role"] in ("owner", "moderator"):
            flash(t("flash.you_cannot_remove_a_moderator_or_owner"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        # Nobody can kick the group owner: ownership must be transferred first
        if target_membership["role"] == "owner":
            flash(t("flash.the_group_owner_cannot_be_removed_transfer_ownership"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target_user = db.get_user_by_id(target_id)
        target_name = target_user["username"] if target_user else "Unknown"
        permanent = request.form.get("permanent", "") == "1"
        if permanent:
            db.ban_group_member(group_id, target_id)
            db.send_group_system_message(group_id, f"{target_name} was banned by {user['username']}")
            notify_change("group_ban", f"{target_name} banned from group '{group['name']}' by {user['username']}")
            log_action("group_ban", request, user=user, group_id=group_id, target_user_id=target_id)
            flash(t("flash.targetname_has_been_banned_from_the_group_they", target_name=target_name), "success")
        else:
            db.remove_group_member(group_id, target_id)
            db.send_group_system_message(group_id, f"{target_name} was removed by {user['username']}")
            notify_change("group_kick", f"{target_name} removed from group '{group['name']}' by {user['username']}")
            log_action("group_kick", request, user=user, group_id=group_id, target_user_id=target_id)
            flash(t("flash.targetname_has_been_removed_from_the_group_they", target_name=target_name), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/promote", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def group_promote(group_id):
        """Promote a group member to moderator (owner or site admin)."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        if my_role != "owner" and not is_site_admin:
            flash(t("flash.only_the_group_owner_can_promote_members"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target_id = request.form.get("user_id", "")
        target_membership = db.get_group_member(group_id, target_id)
        if not target_membership:
            flash(t("flash.user_is_not_a_member_of_the_group"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        if target_membership["role"] != "member":
            flash(t("flash.user_is_already_a_moderator_or_owner"), "info")
            return redirect(url_for("group_view", group_id=group_id))
        target_user = db.get_user_by_id(target_id)
        target_name = target_user["username"] if target_user else "Unknown"
        db.set_group_member_role(group_id, target_id, "moderator")
        db.send_group_system_message(group_id, f"{target_name} was promoted to moderator by {user['username']}")
        notify_change("group_promote", f"{target_name} promoted to moderator in '{group['name']}' by {user['username']}")
        log_action("group_promote", request, user=user, group_id=group_id, target_user_id=target_id)
        flash(t("flash.targetname_is_now_a_moderator", target_name=target_name), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/demote", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def group_demote(group_id):
        """Demote a group moderator back to regular member (owner or site admin)."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        if my_role != "owner" and not is_site_admin:
            flash(t("flash.only_the_group_owner_can_demote_moderators"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target_id = request.form.get("user_id", "")
        target_membership = db.get_group_member(group_id, target_id)
        if not target_membership or target_membership["role"] != "moderator":
            flash(t("flash.user_is_not_a_moderator"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target_user = db.get_user_by_id(target_id)
        target_name = target_user["username"] if target_user else "Unknown"
        db.set_group_member_role(group_id, target_id, "member")
        db.send_group_system_message(group_id, f"{target_name} was demoted to member by {user['username']}")
        notify_change("group_demote", f"{target_name} demoted in '{group['name']}' by {user['username']}")
        log_action("group_demote", request, user=user, group_id=group_id, target_user_id=target_id)
        flash(t("flash.targetname_is_no_longer_a_moderator", target_name=target_name), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/self-downgrade", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def group_self_downgrade(group_id):
        """Allow a moderator to voluntarily downgrade themselves to member."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role = db.get_group_member_role(group_id, user["id"])

        # Check if user is a member of the group
        if my_role is None:
            flash(t("flash.you_are_not_a_member_of_this_group"), "error")
            return redirect(url_for("group_view", group_id=group_id))

        # Check if user is a moderator (only moderators can self-downgrade)
        if my_role != "moderator":
            if my_role == "owner":
                flash(t("flash.owners_cannot_selfdowngrade_transfer_ownership_first"), "error")
            else:
                flash(t("flash.you_are_already_a_member"), "info")
            return redirect(url_for("group_view", group_id=group_id))

        # Perform the downgrade
        db.set_group_member_role(group_id, user["id"], "member")
        db.send_group_system_message(group_id, f"{user['username']} downgraded to member")
        notify_change("group_self_downgrade", f"{user['username']} self-downgraded in '{group['name']}'")
        log_action("group_self_downgrade", request, user, group=group['name'])
        flash(t("flash.you_are_now_a_regular_member"), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/transfer", methods=["POST"])
    @login_required
    @rate_limit(5, 60)
    def group_transfer(group_id):
        """Transfer group ownership to another member (current owner only)."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        if group["is_global"]:
            flash(t("flash.cannot_transfer_ownership_of_the_global_chat"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        my_role = db.get_group_member_role(group_id, user["id"])
        if my_role != "owner":
            flash(t("flash.only_the_group_owner_can_transfer_ownership"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target_id = request.form.get("user_id", "")
        if target_id == user["id"]:
            flash(t("flash.you_already_own_this_group"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target_membership = db.get_group_member(group_id, target_id)
        if not target_membership:
            flash(t("flash.user_is_not_a_member_of_the_group"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target_user = db.get_user_by_id(target_id)
        target_name = target_user["username"] if target_user else "Unknown"
        db.transfer_group_ownership(group_id, user["id"], target_id)
        db.send_group_system_message(group_id, f"{user['username']} transferred ownership to {target_name}")
        notify_change("group_transfer", f"Ownership of '{group['name']}' transferred to {target_name} by {user['username']}")
        log_action("group_transfer", request, user=user, group_id=group_id, target_user_id=target_id)
        flash(t("flash.ownership_transferred_to_targetname_you_are_now_a", target_name=target_name), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/timeout", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def group_timeout(group_id):
        """Temporarily mute a group member for a specified number of minutes."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        # For global chat, site admins can moderate; for regular groups, owner/mod
        if group["is_global"]:
            if not is_site_admin:
                flash(t("flash.only_site_admins_can_moderate_the_global_chat"), "error")
                return redirect(url_for("group_view", group_id=group_id))
        else:
            if my_role not in ("owner", "moderator") and not is_site_admin:
                flash(t("flash.permission_denied"), "error")
                return redirect(url_for("group_view", group_id=group_id))
        target_id = request.form.get("user_id", "")
        if target_id == user["id"]:
            flash(t("flash.you_cannot_time_yourself_out"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target_membership = db.get_group_member(group_id, target_id)
        if not target_membership:
            flash(t("flash.user_is_not_a_member_of_the_group"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        # Don't allow timing out owner or same/higher rank
        if not group["is_global"] and my_role == "moderator" and target_membership["role"] in ("owner", "moderator"):
            flash(t("flash.you_cannot_timeout_a_moderator_or_owner"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        duration = request.form.get("duration", "").strip()
        target_user = db.get_user_by_id(target_id)
        target_name = target_user["username"] if target_user else "Unknown"
        if duration == "indefinite":
            # Far future date for indefinite timeout
            until = "9999-12-31T23:59:59+00:00"
            db.set_group_member_timeout(group_id, target_id, until)
            db.send_group_system_message(group_id, f"{target_name} was timed out indefinitely by {user['username']}")
        elif duration:
            try:
                minutes = int(duration)
                if minutes < 1:
                    raise ValueError
            except (ValueError, TypeError):
                flash(t("flash.the_specified_duration_is_invalid"), "error")
                return redirect(url_for("group_view", group_id=group_id))
            until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
            db.set_group_member_timeout(group_id, target_id, until)
            db.send_group_system_message(group_id, f"{target_name} was timed out for {minutes} minute(s) by {user['username']}")
        else:
            flash(t("flash.please_specify_a_duration"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        notify_change("group_timeout", f"{target_name} timed out in '{group['name']}' by {user['username']}")
        log_action("group_timeout", request, user=user, group_id=group_id, target_user_id=target_id)
        flash(t("flash.targetname_has_been_timed_out", target_name=target_name), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/untimeout", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def group_untimeout(group_id):
        """Remove a timeout from a group member, restoring their ability to send messages."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        if group["is_global"]:
            if not is_site_admin:
                flash(t("flash.only_site_admins_can_moderate_the_global_chat"), "error")
                return redirect(url_for("group_view", group_id=group_id))
        else:
            if my_role not in ("owner", "moderator") and not is_site_admin:
                flash(t("flash.permission_denied"), "error")
                return redirect(url_for("group_view", group_id=group_id))
        target_id = request.form.get("user_id", "")
        target_membership = db.get_group_member(group_id, target_id)
        if not target_membership:
            flash(t("flash.user_is_not_a_member_of_the_group"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target_user = db.get_user_by_id(target_id)
        target_name = target_user["username"] if target_user else "Unknown"
        db.set_group_member_timeout(group_id, target_id, None)
        db.send_group_system_message(group_id, f"{target_name}'s timeout was removed by {user['username']}")
        log_action("group_untimeout", request, user=user, group_id=group_id, target_user_id=target_id)
        flash(t("flash.targetnames_timeout_has_been_removed", target_name=target_name), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/delete_message", methods=["POST"])
    @login_required
    @rate_limit(30, 60)
    def group_delete_message(group_id):
        """Delete a specific message from a group chat (moderator/owner or site admin only)."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role, is_site_admin, can_delete_any_message = _group_message_permissions(group, user)
        if not db.is_group_member(group_id, user["id"]) and not is_site_admin:
            flash(t("flash.you_are_not_a_member_of_this_group"), "error")
            return redirect(url_for("group_list"))
        message_id = request.form.get("message_id", type=int)
        if not message_id:
            flash(t("flash.the_specified_message_is_invalid"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        msg = db.get_group_message_by_id(message_id)
        if not msg or msg["group_id"] != group_id:
            flash(t("flash.the_specified_message_was_not_found"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        if msg["is_system"]:
            flash(t("flash.system_messages_cannot_be_deleted"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        is_own_message = msg["sender_id"] == user["id"]
        if not can_delete_any_message and not is_own_message:
            error_message = (
                "Only site admins can delete other members' messages in the global chat."
                if group["is_global"]
                else "You do not have the required permissions to delete this message."
            )
            flash(error_message, "error")
            return redirect(url_for("group_view", group_id=group_id))
        if msg.get("is_deleted"):
            flash(t("flash.message_has_already_been_deleted"), "info")
            return redirect(url_for("group_view", group_id=group_id))
        db.delete_group_message(message_id)
        db.send_group_system_message(group_id, f"A message was deleted by {user['username']}")
        flash(t("flash.message_deleted_successfully"), "success")
        log_action("group_delete_message", request, user=user, group_id=group_id, message_id=message_id)
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/admin/groups")
    @login_required
    @admin_required
    def admin_groups():
        """Admin: list all group chats for monitoring."""
        groups = db.get_all_group_chats_admin()
        return render_template("admin/groups.html", groups=groups)

    @app.route("/admin/groups/<int:group_id>")
    @login_required
    @admin_required
    def admin_group_view(group_id):
        """Admin: view messages and members of a specific group chat."""
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        messages = _prepare_group_messages(db.get_group_messages(group_id), include_deleted_content=True)
        members = db.get_group_members(group_id)
        return render_template("admin/group_view.html", group=group,
                               messages=messages, members=members)

    # Group recovery actions: unban, invite-code regeneration, admin takeover
    # and chat disable.  Each is reachable without the group owner's
    # cooperation, which is why they all re-check the caller's authority.

    @app.route("/groups/<int:group_id>/unban", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def group_unban(group_id):
        """Revoke a ban and allow a previously banned user to rejoin a group."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        if group["is_global"]:
            if not is_site_admin:
                flash(t("flash.only_site_admins_can_moderate_the_global_chat"), "error")
                return redirect(url_for("group_view", group_id=group_id))
        else:
            if my_role not in ("owner", "moderator") and not is_site_admin:
                flash(t("flash.permission_denied"), "error")
                return redirect(url_for("group_view", group_id=group_id))
        target_id = request.form.get("user_id", "")
        # A banned moderator no longer has a group role (see db/_groups.py),
        # so the check above already stops it.  Refuse self-unbans explicitly
        # as well, so a ban can only ever be lifted by someone else.
        if target_id == user["id"] and not is_site_admin:
            flash(t("flash.permission_denied"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        if not db.is_group_member_banned(group_id, target_id):
            flash(t("flash.user_is_not_banned_from_the_group"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        target_user = db.get_user_by_id(target_id)
        target_name = target_user["username"] if target_user else "Unknown"
        db.unban_group_member(group_id, target_id)
        db.send_group_system_message(group_id, f"{target_name}'s ban was revoked by {user['username']}")
        notify_change("group_unban", f"{target_name} unbanned from group '{group['name']}' by {user['username']}")
        log_action("group_unban", request, user=user, group_id=group_id, target_user_id=target_id)
        flash(t("flash.targetnames_ban_has_been_revoked", target_name=target_name), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/regenerate_code", methods=["POST"])
    @login_required
    @rate_limit(5, 60)
    def group_regenerate_code(group_id):
        """Generate a new invite code for a group chat (owner only).

        Accepts an optional ``custom_code`` form field.  If supplied it must be
        1–32 characters, alphanumeric (letters and digits only), and unique
        across all groups.
        """
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        if my_role != "owner" and not is_site_admin:
            flash(t("flash.only_the_group_owner_or_a_site_admin"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        custom_code = request.form.get("custom_code", "").strip()
        if custom_code:
            if not re.match(r'^[A-Za-z0-9]{1,32}$', custom_code):
                flash(t("flash.custom_invite_code_must_be_between_1_and"), "error")
                return redirect(url_for("group_view", group_id=group_id))
            # Check uniqueness
            existing = db.get_group_chat_by_invite(custom_code)
            if existing and existing["id"] != group_id:
                flash(t("flash.that_invite_code_is_already_in_use_by"), "error")
                return redirect(url_for("group_view", group_id=group_id))
            try:
                new_code = db.regenerate_group_invite_code(group_id, custom_code)
            except db.IntegrityError:
                flash(t("flash.that_invite_code_is_already_in_use_please"), "error")
                return redirect(url_for("group_view", group_id=group_id))
        else:
            new_code = db.regenerate_group_invite_code(group_id)
        db.send_group_system_message(group_id, f"Invite code was regenerated by {user['username']}")
        log_action("group_regenerate_code", request, user=user, group_id=group_id)
        flash(t("flash.invite_code_regenerated_newcode", new_code=new_code), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/export", methods=["GET"])
    @login_required
    @rate_limit(10, 60, exempt_html_nav=False)
    def group_export(group_id):
        """Export group chat messages and attachments as a downloadable file.

        Creates either a text file (if no attachments or small total size) or
        a ZIP archive (if attachments exist or total size is large).
        """
        user = get_current_user()
        if not db.has_permission(user, "chat.group"):
            return _chat_disabled_redirect()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)

        # Check permissions: owner or site admin only
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")

        # Only owners and site admins can export
        if not is_site_admin and my_role != "owner":
            flash(t("flash.only_group_owners_and_site_admins_can_export"), "error")
            return redirect(url_for("group_view", group_id=group_id))

        # Get messages and group info
        messages, group_info = db.get_group_messages_for_export(group_id)
        messages = _prepare_group_messages(messages, include_deleted_content=is_site_admin)

        if not messages:
            flash(t("flash.no_messages_to_export"), "info")
            return redirect(url_for("group_view", group_id=group_id))

        # Generate text content
        text_lines = [
            f"Group Chat Export: {group_info['name']}",
            f"Export Date: {datetime.now(timezone.utc).isoformat()}",
            f"Total Messages: {len(messages)}",
            "=" * 80,
            "",
        ]

        attachment_files = []
        total_attachment_size = 0

        for msg in messages:
            sender = msg.get("sender_name") or "System"
            timestamp = msg["created_at"]
            content = msg["display_content"]
            is_system = msg.get("is_system", False)

            if is_system:
                text_lines.append(f"[{timestamp}] [SYSTEM] {content}")
            else:
                # Sender IPs are moderation data for site admins.  A group
                # owner exporting the chat must not collect members' IPs.
                if is_site_admin:
                    ip = msg.get("ip_address", "N/A")
                    text_lines.append(f"[{timestamp}] {sender} (IP: {ip})")
                else:
                    text_lines.append(f"[{timestamp}] {sender}")
                text_lines.append(f"  {content}")

            # Handle attachments
            if msg.get("display_attachments"):
                text_lines.append("  Attachments:")
                for att in msg["display_attachments"]:
                    att_name = att["original_name"]
                    att_size = att["file_size"]
                    att_filename = att["filename"]
                    text_lines.append(f"    - {att_name} ({att_size / 1024:.1f} KB)")

                    # Collect attachment file paths
                    att_path = os.path.join(config.CHAT_ATTACHMENT_FOLDER, att_filename)
                    if os.path.isfile(att_path):
                        attachment_files.append((att_filename, att_name, att_path, att_size))
                        total_attachment_size += att_size

            text_lines.append("")

        text_content = "\n".join(text_lines)

        # Decide whether to send as text file or ZIP
        # Use ZIP if:
        # - There are attachments, OR
        # - Total size (text + attachments) would exceed 10 MB
        text_size = len(text_content.encode("utf-8"))
        total_size = text_size + total_attachment_size

        # Threshold for unified vs separate: 50 MB (Telegram limit)
        UNIFIED_THRESHOLD = 50 * 1024 * 1024

        safe_group_name = re.sub(r'[^\w\s-]', '', group_info['name']).strip().replace(' ', '_')
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        if not attachment_files and text_size < 10 * 1024 * 1024:
            # Simple text file export
            filename = f"group_chat_{safe_group_name}_{timestamp_str}.txt"
            return send_file(
                io.BytesIO(text_content.encode("utf-8")),
                mimetype="text/plain",
                as_attachment=True,
                download_name=filename,
            )
        elif total_size < UNIFIED_THRESHOLD:
            # Unified ZIP export (everything in one file)
            filename = f"group_chat_{safe_group_name}_{timestamp_str}.zip"
            zip_buffer = io.BytesIO()

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                # Add text content
                zipf.writestr(f"{safe_group_name}_messages.txt", text_content)

                # Add attachments in a subdirectory
                for _att_filename, att_original_name, att_path, _att_size in attachment_files:
                    zipf.write(att_path, f"attachments/{att_original_name}")

            zip_buffer.seek(0)
            return send_file(
                zip_buffer,
                mimetype="application/zip",
                as_attachment=True,
                download_name=filename,
            )
        else:
            # Separate exports - too large for single file
            # Create a manifest and split into multiple ZIPs
            filename = f"group_chat_{safe_group_name}_{timestamp_str}_part1.zip"
            zip_buffer = io.BytesIO()

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                # Add text content
                zipf.writestr(f"{safe_group_name}_messages.txt", text_content)

                # Add a manifest explaining the split
                manifest = {
                    "group_name": group_info['name'],
                    "export_date": datetime.now(timezone.utc).isoformat(),
                    "total_messages": len(messages),
                    "total_attachments": len(attachment_files),
                    "total_size_bytes": total_size,
                    "note": "Attachments are too large to include in a single file. Download them separately from the group chat.",
                }
                zipf.writestr("README.json", json.dumps(manifest, indent=2))

            zip_buffer.seek(0)
            flash(
                t("flash.export_prepared_attachments_are_too_large_totalattachmentsiz", total_attachment_size=total_attachment_size / (1024*1024)),
                "info"
            )
            return send_file(
                zip_buffer,
                mimetype="application/zip",
                as_attachment=True,
                download_name=filename,
            )

    @app.route("/groups/<int:group_id>/clear", methods=["POST"])
    @login_required
    @rate_limit(5, 60)
    def group_clear(group_id):
        """Clear all messages in a group chat. Owners, moderators, and site admins can clear."""
        user = get_current_user()
        if not db.has_permission(user, "chat.group"):
            return _chat_disabled_redirect()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)

        # Check permissions: owner, moderator, or site admin
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")

        if my_role not in ("owner", "moderator") and not is_site_admin:
            flash(t("flash.you_do_not_have_the_required_permissions_to_92ad98"), "error")
            return redirect(url_for("group_view", group_id=group_id))

        messages = db.get_group_messages(group_id)
        message_count = len(messages)
        attachment_count = sum(len(msg.get("attachments") or []) for msg in messages)
        # Delete all messages and attachments
        files_to_delete = db.clear_group_messages(group_id)
        att_dir = config.CHAT_ATTACHMENT_FOLDER
        for fname in files_to_delete:
            try:
                fpath = os.path.join(att_dir, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except OSError:
                pass

        # Send system message
        db.send_group_system_message(group_id, f"Chat cleared by {user['username']}")
        notify_change("group_clear", f"Group '{group['name']}' chat cleared by {user['username']}")
        flash(
            t("flash.group_chat_has_been_cleared_successfully_removed_messagecoun", message_count=message_count, s='' if message_count == 1 else 's', attachment_count=attachment_count, s_2='' if attachment_count == 1 else 's'),
            "success",
        )
        log_action("group_clear", request, user=user, group_id=group_id,
                   message_count=message_count, attachment_count=attachment_count)
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/groups/<int:group_id>/delete", methods=["POST"])
    @login_required
    @rate_limit(5, 60)
    def group_delete(group_id):
        """Permanently delete a non-global group chat (owner or site admin only)."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        if group["is_global"]:
            flash(t("flash.the_global_chat_cannot_be_deleted"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        my_role = db.get_group_member_role(group_id, user["id"])
        is_site_admin = user["role"] in ("admin", "owner")
        if my_role != "owner" and not is_site_admin:
            flash(t("flash.only_the_group_owner_can_delete_this_group"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        group_name = group["name"]
        files = db.delete_group_chat(group_id)
        for fname in files:
            try:
                fpath = os.path.join(config.CHAT_ATTACHMENT_FOLDER, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except OSError:
                pass
        notify_change("group_delete", f"Group '{group_name}' deleted by {user['username']}")
        log_action("group_delete", request, user=user, group_id=group_id)
        flash(t("flash.group_groupname_has_been_deleted", group_name=group_name), "success")
        return redirect(url_for("group_list"))

    @app.route("/groups/<int:group_id>/toggle_active", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def group_toggle_active(group_id):
        """Deactivate or reactivate the global group chat (site admins only).

        When deactivated, members can still read history but cannot send new
        messages.  Messages continue to expire and be cleaned up as normal.
        """
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        if not group["is_global"]:
            flash(t("flash.only_the_global_chat_can_be_deactivated_this"), "error")
            return redirect(url_for("group_view", group_id=group_id))
        user = get_current_user()
        new_state = not bool(group["is_active"])
        db.set_group_chat_active(group_id, new_state)
        action = "reactivated" if new_state else "deactivated"
        db.send_group_system_message(group_id, f"Global chat was {action} by {user['username']}")
        notify_change("group_toggle_active", f"Global chat {action} by {user['username']}")
        flash(t("flash.global_chat_has_been_action", action=action), "success")
        return redirect(url_for("group_view", group_id=group_id))

    @app.route("/admin/groups/<int:group_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_group_delete(group_id):
        """Admin: permanently delete any non-global group chat."""
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        if group["is_global"]:
            flash(t("flash.the_global_chat_cannot_be_deleted"), "error")
            return redirect(url_for("admin_groups"))
        group_name = group["name"]
        user = get_current_user()
        files = db.delete_group_chat(group_id)
        for fname in files:
            try:
                fpath = os.path.join(config.CHAT_ATTACHMENT_FOLDER, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except OSError:
                pass
        notify_change("admin_group_delete", f"Group '{group_name}' deleted by admin {user['username']}")
        log_action("admin_group_delete", request, user=user, group_id=group_id)
        flash(t("flash.group_groupname_has_been_deleted", group_name=group_name), "success")
        return redirect(url_for("admin_groups"))

    @app.route("/groups/<int:group_id>/admin_takeover", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def group_admin_takeover(group_id):
        """Allow a site admin to take ownership of any group."""
        user = get_current_user()
        group = db.get_group_chat(group_id)
        if not group:
            abort(404)
        # A site admin may lift any ban, including its own, so drop a ban row
        # first; otherwise the admin would become an owner that is still
        # marked as banned.
        if db.is_group_member_banned(group_id, user["id"]):
            db.unban_group_member(group_id, user["id"])
        # Join the group if not already a member
        if not db.is_group_member(group_id, user["id"]):
            db.add_group_member(group_id, user["id"])
        # Demote current owner if there is one
        current_members = db.get_group_members(group_id)
        for m in current_members:
            if m["role"] == "owner" and m["user_id"] != user["id"]:
                db.set_group_member_role(group_id, m["user_id"], "moderator")
        # Set admin as owner
        db.set_group_member_role(group_id, user["id"], "owner")
        db.send_group_system_message(group_id, f"{user['username']} (admin) took ownership of the group")
        notify_change("group_admin_takeover", f"{user['username']} took ownership of group '{group['name']}'")
        log_action("group_admin_takeover", request, user=user, group_id=group_id)
        flash(t("flash.you_are_now_the_owner_of_this_group"), "success")
        return redirect(url_for("group_view", group_id=group_id))
