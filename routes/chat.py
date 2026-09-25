"""
BananaWiki: Direct messaging (chat) routes.
"""

from flask import (render_template, request, redirect, url_for, flash, send_file, jsonify, abort,
                   send_from_directory)
import hashlib
import mimetypes
import sqlite3
import json
import os, io, uuid, threading, zipfile, re
from datetime import datetime, timedelta, timezone
from werkzeug.utils import secure_filename
import db
import config
from helpers import (
    login_required, admin_required, get_current_user, rate_limit,
    get_site_timezone, get_effective_chat_cleanup_settings,
    is_joke_audio_extension, get_real_audio_ext,
    get_joke_fail_message, convert_joke_audio,
    t,
)
from wiki_logger import log_action, get_logger
from sync import (
    backup_chats_before_cleanup, backup_group_chats_before_cleanup,
    cleanup_stale_upload_msg_store,
)

DELETED_MESSAGE_PLACEHOLDER = t("chat.deleted_message")


# Types a browser can only show as a picture, a video, a sound or plain text.
# Chat, group chat and kanban attachments are always sent with an attachment
# disposition, but send_file still guesses a Content-Type from the original
# file name, so an uploaded .js or .css would go out as text/javascript or
# text/css on the wiki origin.  The CSP allows script-src 'self', so any
# injected markup could then load the upload as a script or stylesheet, and a
# download disposition does not prevent that.  Every type outside this list
# is sent as application/octet-stream, which a browser will not run or apply
# under the global nosniff header.  Images stay on the list because kanban
# ticket descriptions embed uploaded images through the download URL.
_INLINE_SAFE_ATTACHMENT_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "image/bmp",
    "video/mp4", "video/webm", "video/ogg", "video/quicktime",
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/x-wav", "audio/webm",
    "audio/flac", "audio/aac", "audio/mp4",
    "text/plain",
})


def attachment_download_mimetype(filename):
    """Return the Content-Type to send a downloaded attachment named *filename* with.

    Shared by the DM, group chat and kanban attachment downloads.
    """
    guessed = (mimetypes.guess_type(filename or "")[0] or "").lower()
    if guessed in _INLINE_SAFE_ATTACHMENT_TYPES:
        return guessed
    return "application/octet-stream"


def register_chat_routes(app):
    """Register direct messaging routes on the Flask app."""

    def _chat_allowed_file(filename, settings=None):
        """Delegate to the shared ``allowed_chat_file`` helper."""
        from helpers._validation import allowed_chat_file
        return allowed_chat_file(filename, settings=settings)

    def _chat_disabled_redirect(endpoint="home", **values):
        """Redirect users whose chat access has been disabled."""
        flash(t("flash.your_chat_privileges_have_been_disabled_by_an"), "error")
        return redirect(url_for(endpoint, **values))

    def _chat_message_state(chat):
        """Return rendered chat HTML and lightweight counters for live refresh."""
        messages = db.get_chat_messages(chat["id"])
        attachment_count = sum(len(msg.get("attachments") or []) for msg in messages)
        rendered_messages = _prepare_chat_messages(messages)
        latest_message_id = rendered_messages[-1]["id"] if rendered_messages else 0
        html = render_template("chats/_messages.html", chat=chat, messages=rendered_messages)
        return rendered_messages, {
            "html": html,
            "message_count": len(rendered_messages),
            "attachment_count": attachment_count,
            "latest_message_id": latest_message_id,
            "state_token": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        }

    def _prepare_chat_messages(messages, include_deleted_content=False):
        """Return chat messages annotated for display.

        When ``include_deleted_content`` is False, deleted messages are
        replaced with a placeholder and their attachments are hidden.
        When True, the original deleted content and attachments remain visible
        for admin monitoring views.
        """
        prepared_messages = []
        for msg in messages:
            prepared = dict(msg)
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

    def _remove_chat_files(filenames):
        """Delete chat attachment files from disk, ignoring missing files."""
        for fname in filenames:
            try:
                fpath = os.path.join(config.CHAT_ATTACHMENT_FOLDER, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except OSError:
                pass

    @app.route("/chats")
    @login_required
    def chat_list():
        """List all direct message conversations for the current user."""
        user = get_current_user()
        if not db.has_permission(user, "chat.dm"):
            return _chat_disabled_redirect()
        settings = db.get_site_settings()
        if settings and not settings["chat_dm_enabled"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.direct_messaging_is_currently_disabled"), "error")
            return redirect(url_for("home"))
        chats = db.get_user_chats(user["id"])
        total_unread_dm = db.get_total_unread_dm_count(user["id"])
        return render_template("chats/list.html", chats=chats,
                               total_unread_dm=total_unread_dm)

    @app.route("/chats/new", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 60)
    def chat_new():
        """Start a new direct message conversation with another user."""
        user = get_current_user()
        if not db.has_permission(user, "chat.dm"):
            return _chat_disabled_redirect()
        settings = db.get_site_settings()
        if settings and not settings["chat_dm_enabled"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.direct_messaging_is_currently_disabled"), "error")
            return redirect(url_for("home"))
        if settings and not settings["chat_allow_dm_creation"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.creating_new_direct_message_conversations_is_currently_disab"), "error")
            return redirect(url_for("chat_list"))
        if request.method == "POST":
            target_username = request.form.get("username", "").strip()
            if not target_username:
                flash(t("flash.please_enter_a_username"), "error")
                return redirect(url_for("chat_new"))
            target = db.get_user_by_username(target_username)
            if not target:
                flash(t("flash.user_not_found"), "error")
                return redirect(url_for("chat_new"))
            if target["id"] == user["id"]:
                flash(t("flash.you_cannot_start_a_chat_with_yourself"), "error")
                return redirect(url_for("chat_new"))
            chat = db.get_or_create_chat(user["id"], target["id"])
            return redirect(url_for("chat_view", chat_id=chat["id"]))
        is_admin = user["role"] in ("admin", "owner")
        has_profiles = db.is_plugin_enabled("user_profiles")
        all_users = []
        if has_profiles:
            if is_admin:
                all_users = db.list_all_users_with_profiles()
            else:
                all_users = db.list_published_profiles()
        return render_template("chats/new.html", all_users=all_users, is_admin=is_admin, has_profiles=has_profiles)

    @app.route("/chats/<int:chat_id>")
    @login_required
    def chat_view(chat_id):
        """View and load messages in a direct message conversation."""
        user = get_current_user()
        if not db.has_permission(user, "chat.dm"):
            return _chat_disabled_redirect()
        settings = db.get_site_settings()
        if settings and not settings["chat_dm_enabled"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.direct_messaging_is_currently_disabled"), "error")
            return redirect(url_for("home"))
        if not db.is_chat_participant(chat_id, user["id"]):
            flash(t("flash.access_denied"), "error")
            return redirect(url_for("chat_list"))
        chat = db.get_chat_by_id(chat_id)
        if not chat:
            abort(404)
        # Reset unread count when viewing the chat
        db.reset_unread_count(chat_id, user["id"])
        # Determine other user
        other_id = chat["user2_id"] if chat["user1_id"] == user["id"] else chat["user1_id"]
        other_user = db.get_user_by_id(other_id)
        messages, message_state = _chat_message_state(chat)
        cleanup_settings = get_effective_chat_cleanup_settings(settings)
        return render_template("chats/chat.html", chat=chat, messages=messages,
                               other_user=other_user, message_state=message_state,
                                show_cleanup_banner=(
                                    settings and settings["chat_cleanup_enabled"]
                                    and (
                                       cleanup_settings["dm"]["auto_clear_messages"]
                                       or cleanup_settings["dm"]["auto_clear_attachments"]
                                   )
                               ),
                               clear_attachment_count=message_state["attachment_count"])

    @app.route("/chats/<int:chat_id>/messages")
    @login_required
    @rate_limit(60, 60)
    def chat_messages_partial(chat_id):
        """Return the latest direct-message HTML for live refresh."""
        user = get_current_user()
        if not db.has_permission(user, "chat.dm"):
            return jsonify({"error": "You do not have permission to use direct messaging."}), 403
        settings = db.get_site_settings()
        if settings and not settings["chat_dm_enabled"] and user["role"] not in ("admin", "owner"):
            return jsonify({"error": "Direct messaging is currently disabled."}), 403
        if not db.is_chat_participant(chat_id, user["id"]):
            return jsonify({"error": "Access denied."}), 403
        chat = db.get_chat_by_id(chat_id)
        if not chat:
            abort(404)
        db.reset_unread_count(chat_id, user["id"])
        _, message_state = _chat_message_state(chat)
        return jsonify(message_state)

    @app.route("/chats/<int:chat_id>/send", methods=["POST"])
    @login_required
    @rate_limit(30, 60)
    def chat_send(chat_id):
        """Send a message (and optional file attachment) in a direct message conversation."""
        user = get_current_user()
        if not db.has_permission(user, "chat.dm"):
            return _chat_disabled_redirect()
        settings = db.get_site_settings()
        if settings and not settings["chat_dm_enabled"] and user["role"] not in ("admin", "owner"):
            flash(t("flash.direct_messaging_is_currently_disabled"), "error")
            return redirect(url_for("home"))
        if not db.is_chat_participant(chat_id, user["id"]):
            flash(t("flash.access_denied"), "error")
            return redirect(url_for("chat_list"))

        # Get chat object for finding the other participant
        chat = db.get_chat_by_id(chat_id)
        if not chat:
            flash(t("flash.chat_not_found"), "error")
            return redirect(url_for("chat_list"))

        content = request.form.get("content", "").strip()
        if not content:
            flash(t("flash.message_cannot_be_empty"), "error")
            return redirect(url_for("chat_view", chat_id=chat_id))
        max_msg_len = settings["chat_max_message_length"] if settings and settings["chat_max_message_length"] else 5000
        if len(content) > max_msg_len:
            flash(t("flash.message_cannot_exceed_maxmsglen_characters", max_msg_len=max_msg_len), "error")
            return redirect(url_for("chat_view", chat_id=chat_id))

        # Validate and write file attachment *before* committing the message
        # so that a rejected attachment never leaves a phantom text message.
        attachment_info = None
        if "attachment" in request.files:
            f = request.files["attachment"]
            if f.filename:
                # Check if attachments are enabled
                if settings and not settings["chat_attachments_enabled"] and user["role"] not in ("admin", "owner"):
                    flash(t("flash.file_attachments_are_currently_disabled"), "error")
                    return redirect(url_for("chat_view", chat_id=chat_id))
                # Check daily limit from site settings
                max_attachments = settings["chat_attachments_per_day_limit"] if settings and settings["chat_attachments_per_day_limit"] else 10
                att_count = db.get_user_chat_attachment_count_today(user["id"])
                if att_count >= max_attachments:
                    flash(t("flash.daily_attachment_limit_of_maxattachments_files_reached", max_attachments=max_attachments), "error")
                    return redirect(url_for("chat_view", chat_id=chat_id))
                if not _chat_allowed_file(f.filename, settings=settings):
                    flash(t("flash.file_type_not_allowed"), "error")
                    return redirect(url_for("chat_view", chat_id=chat_id))
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
                    except OSError as exc:
                        get_logger().warning("Failed to remove oversized chat attachment: %s", exc, exc_info=True)
                    flash(t("flash.file_exceeds_the_maxattsizemb_mb_limit", max_att_size_mb=max_att_size_mb), "error")
                    return redirect(url_for("chat_view", chat_id=chat_id))
                # Joke audio: convert mp5/mp7 → mp3 via ffmpeg
                if is_joke:
                    ok, err = convert_joke_audio(filepath, filepath)
                    if not ok:
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        flash(f"{get_joke_fail_message(ext)} {err or ''}", "error")
                        return redirect(url_for("chat_view", chat_id=chat_id))
                    get_logger().info("Joke audio %s converted to %s", ext, stored_ext)
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
                except Exception as exc:
                    get_logger().debug("Chat blob storage failed (non-critical): %s", exc)

        ip_address = request.remote_addr or "unknown"
        msg_id = db.send_chat_message(chat_id, user["id"], content, ip_address)

        # Increment unread count for the other participant
        other_id = chat["user2_id"] if chat["user1_id"] == user["id"] else chat["user1_id"]
        db.increment_unread_count(chat_id, other_id)

        if attachment_info:
            db.add_chat_attachment(msg_id, *attachment_info)

        log_action("chat_send", request, user=user, chat_id=chat_id)
        return redirect(url_for("chat_view", chat_id=chat_id))

    @app.route("/chats/<int:chat_id>/delete_message", methods=["POST"])
    @login_required
    @rate_limit(30, 60)
    def chat_delete_message(chat_id):
        """Delete a specific direct message from a chat."""
        user = get_current_user()
        if not db.has_permission(user, "chat.dm"):
            return _chat_disabled_redirect()
        chat = db.get_chat_by_id(chat_id)
        if not chat:
            abort(404)
        if not db.is_chat_participant(chat_id, user["id"]):
            flash(t("flash.access_denied"), "error")
            return redirect(url_for("chat_list"))
        message_id = request.form.get("message_id", type=int)
        if not message_id:
            flash(t("flash.the_specified_message_is_invalid"), "error")
            return redirect(url_for("chat_view", chat_id=chat_id))
        msg = db.get_chat_message_by_id(message_id)
        if not msg or msg["chat_id"] != chat_id:
            flash(t("flash.the_specified_message_was_not_found"), "error")
            return redirect(url_for("chat_view", chat_id=chat_id))
        if msg["sender_id"] != user["id"]:
            flash(t("flash.you_do_not_have_the_required_permissions_to_306df2"), "error")
            return redirect(url_for("chat_view", chat_id=chat_id))
        if msg.get("is_deleted"):
            flash(t("flash.message_has_already_been_deleted"), "info")
            return redirect(url_for("chat_view", chat_id=chat_id))
        db.delete_chat_message(message_id)
        flash(t("flash.message_has_been_successfully_deleted"), "success")
        log_action("chat_delete_message", request, user=user, chat_id=chat_id, message_id=message_id)
        return redirect(url_for("chat_view", chat_id=chat_id))

    @app.route("/chats/attachments/<int:attachment_id>/download")
    @login_required
    def chat_attachment_download(attachment_id):
        """Download a file attachment from a direct message conversation."""
        att = db.get_chat_attachment(attachment_id)
        if not att:
            abort(404)
        user = get_current_user()
        if not db.has_permission(user, "chat.dm"):
            abort(403)
        is_admin = user["role"] in ("admin", "owner")
        if att["is_deleted"] and not is_admin:
            abort(403)
        if not is_admin and not db.is_chat_participant(att["chat_id"], user["id"]):
            abort(403)
        att_dir = os.path.abspath(config.CHAT_ATTACHMENT_FOLDER)
        # Try DB blob first, then fall back to disk
        blob_id = att["blob_id"] if "blob_id" in att.keys() else None
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

    @app.route("/chats/<int:chat_id>/export", methods=["GET"])
    @login_required
    @rate_limit(10, 60, exempt_html_nav=False)
    def chat_export(chat_id):
        """Export direct message chat messages and attachments as a downloadable file.

        Both participants can export their chat history.
        """
        user = get_current_user()
        if not db.has_permission(user, "chat.dm"):
            return _chat_disabled_redirect()
        chat = db.get_chat_by_id(chat_id)
        if not chat:
            abort(404)

        # Check permissions: both participants can export
        if not db.is_chat_participant(chat_id, user["id"]):
            flash(t("flash.access_denied"), "error")
            return redirect(url_for("chat_list"))

        # Get messages and chat info
        messages = _prepare_chat_messages(db.get_chat_messages(chat_id))
        user1 = db.get_user_by_id(chat["user1_id"])
        user2 = db.get_user_by_id(chat["user2_id"])
        user1_name = user1["username"] if user1 else "[deleted user]"
        user2_name = user2["username"] if user2 else "[deleted user]"

        if not messages:
            flash(t("flash.no_messages_to_export"), "info")
            return redirect(url_for("chat_view", chat_id=chat_id))

        # Generate text content
        text_lines = [
            "Direct Message Export",
            f"Participants: {user1_name} & {user2_name}",
            f"Export Date: {datetime.now(timezone.utc).isoformat()}",
            f"Total Messages: {len(messages)}",
            "=" * 80,
            "",
        ]

        attachment_files = []
        total_attachment_size = 0
        # The sender IP is moderation data, shown only in the admin views.
        # A participant's export must not hand them the other person's IP.
        include_ip = user["role"] in ("admin", "owner")

        for msg in messages:
            sender = msg.get("sender_name", "Unknown")
            timestamp = msg["created_at"]
            content = msg["display_content"]

            if include_ip:
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
        text_size = len(text_content.encode("utf-8"))
        total_size = text_size + total_attachment_size
        # Beyond this the archive is built entirely in memory, so the export
        # ships the transcript and points at the attachments instead. The group
        # export applies the same limit.
        UNIFIED_THRESHOLD = 50 * 1024 * 1024

        # Create safe filename
        other_username = user2_name if user["id"] == chat["user1_id"] else user1_name
        safe_name = re.sub(r'[^\w\s-]', '', other_username).strip().replace(' ', '_')
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        # Simple text file if no attachments and small size
        if not attachment_files and text_size < 10 * 1024 * 1024:
            filename = f"dm_{safe_name}_{timestamp_str}.txt"
            return send_file(
                io.BytesIO(text_content.encode("utf-8")),
                mimetype="text/plain",
                as_attachment=True,
                download_name=filename,
            )
        elif total_size < UNIFIED_THRESHOLD:
            # ZIP with messages and attachments
            filename = f"dm_{safe_name}_{timestamp_str}.zip"
            zip_buffer = io.BytesIO()

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                # Add text content
                zipf.writestr(f"dm_{safe_name}_messages.txt", text_content)

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
            filename = f"dm_{safe_name}_{timestamp_str}_part1.zip"
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                zipf.writestr(f"dm_{safe_name}_messages.txt", text_content)
                manifest = {
                    "chat_with": other_username,
                    "export_date": datetime.now(timezone.utc).isoformat(),
                    "total_messages": len(messages),
                    "total_attachments": len(attachment_files),
                    "total_size_bytes": total_size,
                    "note": "Attachments are too large to include in a single file. Download them separately from the chat.",
                }
                zipf.writestr("README.json", json.dumps(manifest, indent=2))
            zip_buffer.seek(0)
            flash(
                t("flash.export_prepared_attachments_are_too_large_totalattachmentsiz", total_attachment_size=total_attachment_size / (1024 * 1024)),
                "info"
            )
            return send_file(
                zip_buffer,
                mimetype="application/zip",
                as_attachment=True,
                download_name=filename,
            )

    @app.route("/chats/<int:chat_id>/clear", methods=["POST"])
    @login_required
    @rate_limit(5, 60)
    def chat_clear(chat_id):
        """Clear all messages in a direct message chat. Both participants can clear the chat."""
        user = get_current_user()
        if not db.has_permission(user, "chat.dm"):
            return _chat_disabled_redirect()
        chat = db.get_chat_by_id(chat_id)
        if not chat:
            abort(404)

        # Check permissions: both participants can clear
        if not db.is_chat_participant(chat_id, user["id"]):
            flash(t("flash.access_denied"), "error")
            return redirect(url_for("chat_list"))

        messages = db.get_chat_messages(chat_id)
        message_count = len(messages)
        attachment_count = sum(len(msg.get("attachments") or []) for msg in messages)
        # Delete all messages and attachments
        files_to_delete = db.clear_chat_messages(chat_id)
        _remove_chat_files(files_to_delete)
        flash(
            t("flash.chat_has_been_successfully_cleared_removed_messagecount_mess", message_count=message_count, s='' if message_count == 1 else 's', attachment_count=attachment_count, s_2='' if attachment_count == 1 else 's'),
            "success",
        )
        log_action("chat_clear", request, user=user, chat_id=chat_id,
                   message_count=message_count, attachment_count=attachment_count)
        return redirect(url_for("chat_view", chat_id=chat_id))

    @app.route("/admin/chats")
    @login_required
    @admin_required
    def admin_chats():
        """Admin: browse all direct message conversations, optionally filtered by user."""
        user_filter = request.args.get("user_id")
        if user_filter:
            chats = db.get_user_chats_admin(user_filter)
            filter_user = db.get_user_by_id(user_filter)
        else:
            chats = db.get_all_chats_admin()
            filter_user = None
        all_users = db.list_users()
        return render_template("admin/chats.html", chats=chats,
                               all_users=all_users, filter_user=filter_user)

    @app.route("/admin/chats/<int:chat_id>")
    @login_required
    @admin_required
    def admin_chat_view(chat_id):
        """Admin: view all messages within a specific direct message conversation."""
        chat = db.get_chat_by_id(chat_id)
        if not chat:
            abort(404)
        messages = _prepare_chat_messages(db.get_chat_messages(chat_id), include_deleted_content=True)
        user1 = db.get_user_by_id(chat["user1_id"])
        user2 = db.get_user_by_id(chat["user2_id"])
        user1_name = user1["username"] if user1 else "[deleted user]"
        user2_name = user2["username"] if user2 else "[deleted user]"
        return render_template("admin/chat_view.html", chat=chat, messages=messages,
                               user1=user1, user2=user2,
                               user1_name=user1_name, user2_name=user2_name)

    _cleanup_timer_holder = [None]

    def _schedule_chat_cleanup():
        """Schedule the next chat cleanup based on configured frequency and hour."""
        if app.testing:
            return
        try:
            site_tz = get_site_timezone()
            now = datetime.now(site_tz)
            settings = db.get_site_settings()

            # Get cleanup schedule from DB settings with sensible defaults
            cleanup_frequency = (settings["chat_cleanup_frequency_days"] if settings and settings["chat_cleanup_frequency_days"] else 7)
            cleanup_hour = (settings["chat_cleanup_hour"] if settings and settings["chat_cleanup_hour"] is not None else 3)

            # Get last cleanup time
            last_cleanup = None
            if settings:
                try:
                    last_cleanup_str = settings["last_chat_cleanup_at"]
                    if last_cleanup_str:
                        # Parse ISO format datetime
                        last_cleanup = datetime.fromisoformat(last_cleanup_str.replace('Z', '+00:00'))
                        # Convert to site timezone
                        last_cleanup = last_cleanup.astimezone(site_tz)
                except (KeyError, ValueError, AttributeError, TypeError):
                    pass

            # If no last cleanup recorded, schedule for next configured hour today/tomorrow
            if not last_cleanup:
                target = now.replace(hour=cleanup_hour, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
            else:
                # Schedule for configured frequency after last cleanup
                target = last_cleanup.replace(hour=cleanup_hour, minute=0, second=0, microsecond=0)
                target += timedelta(days=cleanup_frequency)

                # If target is in the past, schedule for the next interval
                while target <= now:
                    target += timedelta(days=cleanup_frequency)

            delay = (target - now).total_seconds()
            _cleanup_timer_holder[0] = threading.Timer(delay, _run_chat_cleanup)
            _cleanup_timer_holder[0].daemon = True
            _cleanup_timer_holder[0].start()
        except Exception:
            try:
                get_logger().error("Chat cleanup scheduling failed", exc_info=True)
            except Exception:
                pass
            # Retry after 10 s so the chain recovers from transient first-boot failures
            # (e.g. DB not yet initialised when the scheduler first fires).
            t = threading.Timer(10, _schedule_chat_cleanup)
            t.daemon = True
            t.start()
            _cleanup_timer_holder[0] = t

    def _run_chat_cleanup():
        """Apply the configured chat retention policy; backups run separately."""
        if app.testing:
            return
        # Always reschedule first so the timer chain is never broken
        _schedule_chat_cleanup()

        # In multi-worker deployments all workers share the same timer schedule
        # and may fire concurrently.  Use a DB-based claim lock so only one
        # worker actually runs the cleanup.
        try:
            settings_for_lock = db.get_site_settings()
        except (sqlite3.Error, OSError):
            get_logger().exception("Chat cleanup skipped: settings could not be read")
            return
        if settings_for_lock:
            freq_days = settings_for_lock.get("chat_cleanup_frequency_days") or 7
            # Guard window: half the cleanup period, minimum 1 hour
            min_interval = max(int(freq_days) * 86400 / 2, 3600)
        else:
            min_interval = 3600
        if not db.check_and_claim_chat_cleanup(min_interval):
            return

        # Cleanup must be enabled via the chat_cleanup_enabled site setting.
        # The scheduler is now part of the chat plugin, so no separate plugin
        # gate is needed.
        settings = settings_for_lock
        if not settings or not settings["chat_cleanup_enabled"]:
            return

        try:
            backup_chats_before_cleanup()
        except Exception:
            try:
                get_logger().error("Chat backup failed before cleanup", exc_info=True)
            except Exception:
                pass
        try:
            backup_group_chats_before_cleanup()
        except Exception:
            try:
                get_logger().error("Group chat backup failed before cleanup", exc_info=True)
            except Exception:
                pass

        cleanup_settings = get_effective_chat_cleanup_settings(settings)
        dm_settings = cleanup_settings["dm"]
        group_settings = cleanup_settings["group"]

        att_dir = config.CHAT_ATTACHMENT_FOLDER

        # Clean up direct messages using DM-specific settings
        try:
            if dm_settings["auto_clear_messages"] and dm_settings["message_retention_days"] > 0:
                # Clear old messages (this also clears their attachments via CASCADE)
                files_to_delete = db.cleanup_old_chat_messages(dm_settings["message_retention_days"])
                for fname in files_to_delete:
                    try:
                        fpath = os.path.join(att_dir, fname)
                        if os.path.isfile(fpath):
                            os.remove(fpath)
                    except OSError:
                        pass
            elif dm_settings["auto_clear_attachments"] and dm_settings["attachment_retention_days"] > 0:
                # Clear only old attachments (keep messages)
                files_to_delete = db.cleanup_old_chat_attachments(dm_settings["attachment_retention_days"])
                for fname in files_to_delete:
                    try:
                        fpath = os.path.join(att_dir, fname)
                        if os.path.isfile(fpath):
                            os.remove(fpath)
                    except OSError:
                        pass
        except Exception:
            try:
                get_logger().error("DM chat cleanup failed", exc_info=True)
            except Exception:
                pass

        # Clean up group messages using Group-specific settings
        try:
            if group_settings["auto_clear_messages"] and group_settings["message_retention_days"] > 0:
                # Clear old messages (this also clears their attachments via CASCADE)
                group_files = db.cleanup_old_group_messages(group_settings["message_retention_days"])
                for fname in group_files:
                    try:
                        fpath = os.path.join(att_dir, fname)
                        if os.path.isfile(fpath):
                            os.remove(fpath)
                    except OSError:
                        pass
            elif group_settings["auto_clear_attachments"] and group_settings["attachment_retention_days"] > 0:
                # Clear only old attachments (keep messages)
                group_files = db.cleanup_old_group_attachments(group_settings["attachment_retention_days"])
                for fname in group_files:
                    try:
                        fpath = os.path.join(att_dir, fname)
                        if os.path.isfile(fpath):
                            os.remove(fpath)
                    except OSError:
                        pass
        except Exception:
            try:
                get_logger().error("Group chat cleanup failed", exc_info=True)
            except Exception:
                pass

        # Record cleanup timestamp
        try:
            site_tz = get_site_timezone()
            now = datetime.now(site_tz)
            db.update_site_settings(last_chat_cleanup_at=now.isoformat())
        except Exception:
            try:
                get_logger().error("Failed to record chat cleanup timestamp", exc_info=True)
            except Exception:
                pass

        # Run database-wide cleanup to prune orphaned / stale rows
        summary = {}
        try:
            summary = db.run_full_cleanup()
            try:
                get_logger().info(
                    f"DB cleanup completed: {summary}"
                )
            except Exception:
                pass
        except Exception:
            try:
                get_logger().error("DB cleanup failed during scheduled maintenance", exc_info=True)
            except Exception:
                pass

        # Prune stale entries from the Telegram upload-message-ID store
        try:
            cleanup_stale_upload_msg_store()
        except Exception:
            try:
                get_logger().error("Stale upload message store cleanup failed", exc_info=True)
            except Exception:
                pass

        # Clean up orphaned page-attachment files from disk
        try:
            from routes.uploads import cleanup_orphaned_attachments
            cleanup_orphaned_attachments()
        except Exception:
            try:
                get_logger().error("Orphaned attachment cleanup failed", exc_info=True)
            except Exception:
                pass

        # Clean up orphaned chat/group-attachment files from disk
        try:
            from routes.uploads import cleanup_orphaned_chat_attachments
            cleanup_orphaned_chat_attachments()
        except Exception:
            try:
                get_logger().error("Orphaned chat attachment cleanup failed", exc_info=True)
            except Exception:
                pass

        # Clean up orphaned custom-page files from disk
        try:
            from routes.uploads import cleanup_orphaned_custom_page_files
            cleanup_orphaned_custom_page_files()
        except Exception:
            try:
                get_logger().error("Orphaned custom page file cleanup failed", exc_info=True)
            except Exception:
                pass

        # Clean up unused upload images (not referenced in any content)
        try:
            from routes.uploads import cleanup_unused_uploads
            cleanup_unused_uploads()
        except Exception:
            try:
                get_logger().error("Unused upload cleanup failed", exc_info=True)
            except Exception:
                pass

        # Notify Telegram about the completed maintenance cycle
        try:
            from sync import notify_cleanup_completed
            notify_cleanup_completed(summary)
        except Exception:
            try:
                get_logger().error("Sync cleanup-completed notification failed", exc_info=True)
            except Exception:
                pass

    _schedule_chat_cleanup()
