"""
BananaWiki: File upload and download routes.
"""

from flask import (request, redirect, url_for, flash,
                   send_file, jsonify, abort)
import logging
import os, io, uuid, zipfile, sqlite3
from werkzeug.utils import secure_filename
from PIL import Image
import db
import config
from db._canvas import referenced_upload_filenames as canvas_referenced_upload_filenames

_logger = logging.getLogger("bananawiki")
from helpers import (
    login_required, editor_required, get_current_user,
    allowed_file, allowed_attachment, get_effective_max_upload_size,
    rate_limit, editor_has_category_access,
    user_can_view_page, _safe_ext,
    is_joke_audio_extension, get_real_audio_ext,
    get_joke_success_message, get_joke_fail_message, convert_joke_audio,
    t, user_can_use_page_builder,
)
import wiki_logger
from sync import notify_change, notify_file_upload, notify_file_deleted


def cleanup_unused_uploads():
    """Delete uploaded image files that are not referenced anywhere.

    Called after draft deletion, page commit, page creation, and page deletion
    so that images uploaded but never committed (or removed before committing)
    are automatically purged.  Images still present in page content, revision
    history, unsaved drafts, announcements, custom pages, or canvas layouts
    and their history are preserved.

    Returns the number of files removed.
    """
    if not os.path.isdir(config.UPLOAD_FOLDER):
        return 0
    # Canvas image nodes point at uploads as well.  Their layouts are checked
    # even while the canvas plugin is disabled, so turning it back on finds
    # the images still there.
    referenced = set(db.get_all_referenced_image_filenames())
    referenced |= canvas_referenced_upload_filenames()
    removed = 0
    for fname in os.listdir(config.UPLOAD_FOLDER):
        if fname.startswith("."):
            continue
        if fname not in referenced:
            fpath = os.path.join(config.UPLOAD_FOLDER, fname)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                    notify_file_deleted(fname)
                    removed += 1
                except OSError:
                    _logger.warning("Failed to remove orphaned upload: %s", fpath, exc_info=True)
    return removed


def cleanup_orphaned_attachments():
    """Remove page-attachment files from disk that have no matching DB record.

    The ``ATTACHMENT_FOLDER`` may accumulate orphaned files when a page (and
    its ``page_attachments`` rows) is deleted but the on-disk file removal
    fails or is interrupted.  This function reconciles the two.

    Returns the number of orphaned files removed.
    """
    att_dir = getattr(config, "ATTACHMENT_FOLDER", None)
    if not att_dir or not os.path.isdir(att_dir):
        return 0

    with db.get_db_context() as conn:
        rows = conn.execute("SELECT filename FROM page_attachments").fetchall()
    known = {r["filename"] for r in rows}

    removed = 0
    for fname in os.listdir(att_dir):
        if fname.startswith("."):
            continue
        if fname not in known:
            fpath = os.path.join(att_dir, fname)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                    removed += 1
                except OSError:
                    _logger.warning("Failed to remove orphaned attachment: %s", fpath, exc_info=True)
    return removed


def cleanup_orphaned_chat_attachments():
    """Remove DM chat-attachment files from disk that have no matching DB record.

    The ``CHAT_ATTACHMENT_FOLDER`` may accumulate orphaned files when chat
    messages (and their ``chat_attachments`` rows) are deleted but the on-disk
    file removal fails or is skipped.  This function reconciles the two.

    Returns the number of orphaned files removed.
    """
    att_dir = getattr(config, "CHAT_ATTACHMENT_FOLDER", None)
    if not att_dir or not os.path.isdir(att_dir):
        return 0

    with db.get_db_context() as conn:
        # Collect filenames from both DM and group attachment tables since they
        # share the same on-disk folder.
        known = set()
        try:
            rows = conn.execute("SELECT filename FROM chat_attachments").fetchall()
            known.update(r["filename"] for r in rows)
        except sqlite3.OperationalError:
            pass
        try:
            rows = conn.execute("SELECT filename FROM group_attachments").fetchall()
            known.update(r["filename"] for r in rows)
        except sqlite3.OperationalError:
            pass

    removed = 0
    for fname in os.listdir(att_dir):
        if fname.startswith("."):
            continue
        if fname not in known:
            fpath = os.path.join(att_dir, fname)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                    removed += 1
                except OSError:
                    _logger.warning("Failed to remove orphaned chat attachment: %s", fpath, exc_info=True)
    return removed


def cleanup_orphaned_custom_page_files():
    """Remove custom-page files from disk that have no matching DB record.

    The ``CUSTOM_PAGE_FILES_FOLDER`` may accumulate orphaned files when
    custom pages are deleted.  This function reconciles disk files with the
    ``custom_page_files`` table.

    Returns the number of orphaned files removed.
    """
    att_dir = getattr(config, "CUSTOM_PAGE_FILES_FOLDER", None)
    if not att_dir or not os.path.isdir(att_dir):
        return 0

    with db.get_db_context() as conn:
        try:
            rows = conn.execute("SELECT filename FROM custom_page_files").fetchall()
        except sqlite3.OperationalError:
            return 0
    known = {r["filename"] for r in rows}

    removed = 0
    for fname in os.listdir(att_dir):
        if fname.startswith("."):
            continue
        if fname not in known:
            fpath = os.path.join(att_dir, fname)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                    removed += 1
                except OSError:
                    _logger.warning("Failed to remove orphaned custom page file: %s", fpath, exc_info=True)
    return removed


# Page attachments are always downloaded, never shown or run by the browser.
# Without an explicit type, send_file guesses one from the original file
# name, so an uploaded .js would be served as text/javascript and could be
# loaded as a same-origin script (the CSP allows script-src 'self') even
# with an attachment disposition.  octet-stream plus the global nosniff
# header rules that out.
_ATTACHMENT_MIMETYPE = "application/octet-stream"


def _can_delete_attachment(user, attachment):
    """Return True if *user* may delete the page *attachment*.

    ``attachment.delete_any`` covers every attachment and
    ``attachment.delete_own`` covers the ones the user uploaded.  Category
    write access is checked separately by the route.
    """
    if db.has_permission(user, "attachment.delete_any"):
        return True
    return (
        attachment["uploaded_by"] == user["id"]
        and db.has_permission(user, "attachment.delete_own")
    )


def register_upload_routes(app):
    """Register file upload and download routes on the Flask app."""

    @app.route("/api/upload", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def upload_image():
        """Upload an image file for use in wiki pages; returns the URL and filename as JSON."""
        user = get_current_user()
        if user["role"] not in ("editor", "admin", "owner") and not user_can_use_page_builder(user):
            return jsonify({"error": "Access denied"}), 403
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        f = request.files["file"]
        if not f.filename or not allowed_file(f.filename):
            return jsonify({"error": "Invalid file type"}), 400
        # Validate that the file is a genuine image by reading it with Pillow
        try:
            img = Image.open(f.stream)
            img.verify()
            f.stream.seek(0)
        except Exception:
            return jsonify({"error": "File is not a valid image"}), 400
        os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
        ext = _safe_ext(f.filename)
        if not ext:
            return jsonify({"error": "Invalid file extension"}), 400
        filename = f"{uuid.uuid4().hex}.{ext}"
        upload_root = os.path.abspath(config.UPLOAD_FOLDER)
        filepath = os.path.abspath(os.path.normpath(os.path.join(upload_root, filename)))
        if os.path.commonpath([upload_root, filepath]) != upload_root:
            return jsonify({"error": "Invalid upload path"}), 400
        # Per-user daily quota check.  We measure the upload size first so
        # the quota counter is accurate; the file is small enough at this
        # point because ``allowed_file`` already accepted it and Flask's
        # ``MAX_CONTENT_LENGTH`` capped the request body.
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        ok, error = db.check_and_record_upload(user["id"], size)
        if not ok:
            return jsonify({"error": error}), 429
        try:
            f.save(filepath)
        except OSError:
            return jsonify({"error": "Failed to save file"}), 500
        wiki_logger.log_action("upload_image", request, user=user, filename=filename)
        notify_file_upload(filename, filepath)
        url = url_for("static", filename=f"uploads/{filename}")
        return jsonify({"url": url, "filename": filename})

    @app.route("/api/upload/delete", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(10, 60)
    def delete_upload():
        """Delete a previously uploaded image file by filename."""
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "invalid request"}), 400
        filename = data.get("filename", "")
        safe_name = secure_filename(filename)
        if not safe_name:
            return jsonify({"error": "invalid filename"}), 400
        filepath = os.path.join(config.UPLOAD_FOLDER, safe_name)
        upload_root = os.path.abspath(config.UPLOAD_FOLDER)
        filepath = os.path.abspath(os.path.normpath(filepath))
        if os.path.commonpath([upload_root, filepath]) != upload_root:
            return jsonify({"error": "invalid filename"}), 400
        if os.path.isfile(filepath):
            try:
                os.remove(filepath)
            except FileNotFoundError:
                pass  # file was already removed (race condition)
            except OSError:
                return jsonify({"error": "failed to delete file"}), 500
            user = get_current_user()
            wiki_logger.log_action("delete_upload", request, user=user, filename=safe_name)
            notify_file_deleted(safe_name)
        return jsonify({"ok": True})

    @app.route("/api/page/<int:page_id>/attachments", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def upload_attachment(page_id):
        """Upload a file attachment to a wiki page."""
        page = db.get_page(page_id)
        if not page:
            return jsonify({"error": "Page not found"}), 404
        user = get_current_user()
        if not db.has_permission(user, "attachment.upload"):
            return jsonify({"error": t("error.permission_denied")}), 403
        is_editor = user["role"] in ("editor", "admin", "owner")
        if page["is_home"] if "is_home" in page.keys() else False:
            if not is_editor:
                return jsonify({"error": "Only editors and administrators can upload attachments to the home page"}), 403
        elif not editor_has_category_access(user, page["category_id"]):
            return jsonify({"error": "Access denied"}), 403
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        f = request.files["file"]
        settings = db.get_site_settings()
        if not f.filename or not allowed_attachment(f.filename, settings=settings):
            return jsonify({"error": "File type not allowed"}), 400
        # Stream to a temp file while enforcing the size limit
        os.makedirs(config.ATTACHMENT_FOLDER, exist_ok=True)
        ext = _safe_ext(f.filename)
        if not ext:
            return jsonify({"error": "Invalid file extension"}), 400
        is_joke = is_joke_audio_extension(ext)
        stored_ext = get_real_audio_ext(ext) if is_joke else ext
        stored_name = f"{uuid.uuid4().hex}.{stored_ext}"
        attach_root = os.path.abspath(config.ATTACHMENT_FOLDER)
        filepath = os.path.abspath(os.path.join(attach_root, stored_name))
        if os.path.commonpath([attach_root, filepath]) != attach_root:
            return jsonify({"error": "Invalid upload path"}), 400
        max_size = get_effective_max_upload_size(settings)
        file_size = 0
        chunk_size = 64 * 1024  # 64 KB
        _blob_buf = io.BytesIO()  # accumulate content for DB blob (dual-write)
        try:
            with open(filepath, "wb") as out:
                while True:
                    chunk = f.stream.read(chunk_size)
                    if not chunk:
                        break
                    file_size += len(chunk)
                    if file_size > max_size:
                        out.close()
                        os.remove(filepath)
                        limit_mb = max_size // (1024 * 1024)
                        return jsonify({"error": f"File exceeds the {limit_mb} MB limit"}), 413
                    out.write(chunk)
                    _blob_buf.write(chunk)
        except OSError:
            if os.path.isfile(filepath):
                os.remove(filepath)
            return jsonify({"error": "Failed to save file"}), 500
        # Joke audio: convert mp5/mp7 → mp3 via ffmpeg
        if is_joke:
            ok, err = convert_joke_audio(filepath, filepath)
            if not ok:
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                return jsonify({
                    "error": get_joke_fail_message(ext),
                    "detail": err,
                    "joke": True,
                }), 400
            _logger.info("Joke audio %s converted to %s", ext, stored_ext)
        # Per-user daily quota: count attachment uploads against the
        # same window used by /api/upload.  Done after the chunked write
        # so we can charge the actual byte total, not the declared
        # Content-Length.  On quota refusal, delete the file we just
        # wrote so disk space stays consistent with the counter.
        ok, error = db.check_and_record_upload(user["id"], file_size, settings=settings)
        if not ok:
            try:
                if os.path.isfile(filepath):
                    os.remove(filepath)
            except OSError as exc:
                _logger.warning("Failed to remove attachment after quota refusal: %s", exc, exc_info=True)
            return jsonify({"error": error}), 429
        original_name = secure_filename(f.filename)
        # Dual-write: store buffered content in DB blob for DB-first reads.
        # For joke audio, re-read the converted file so the blob matches disk.
        blob_id = None
        try:
            blob_data = _blob_buf.getvalue()
            if is_joke:
                try:
                    with open(filepath, "rb") as cf:
                        blob_data = cf.read()
                except OSError:
                    pass
            blob_id = db.store_blob(stored_name, blob_data, "application/octet-stream")
        except Exception as exc:
            _logger.debug("Attachment blob storage failed (non-critical): %s", exc)
        attachment_id = db.add_page_attachment(page_id, stored_name, original_name, file_size, user["id"], blob_id=blob_id)
        wiki_logger.log_action("upload_attachment", request, user=user, page=page["slug"], filename=original_name)
        notify_change("attachment_upload", f"Attachment '{original_name}' uploaded to page '{page['slug']}'")
        notify_file_upload(stored_name, filepath, display_name=original_name)
        resp = {"id": attachment_id, "name": original_name, "size": file_size}
        if is_joke:
            resp["joke"] = True
            resp["joke_message"] = get_joke_success_message(ext)
        return jsonify(resp)

    @app.route("/api/attachments/<int:attachment_id>", methods=["DELETE"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def delete_attachment(attachment_id):
        """Delete a page attachment."""
        attachment = db.get_page_attachment(attachment_id)
        if not attachment:
            return jsonify({"error": "Not found"}), 404
        page = db.get_page(attachment["page_id"])
        user = get_current_user()
        if not _can_delete_attachment(user, attachment):
            return jsonify({"error": t("error.permission_denied")}), 403
        is_admin = user["role"] in ("admin", "owner")
        if page and (page["is_home"] if "is_home" in page.keys() else False):
            if not is_admin:
                return jsonify({"error": "Only administrators can delete attachments from the home page"}), 403
        elif not editor_has_category_access(user, page["category_id"]):
            return jsonify({"error": "Access denied"}), 403
        filepath = os.path.join(config.ATTACHMENT_FOLDER, attachment["filename"])
        attach_root = os.path.abspath(config.ATTACHMENT_FOLDER)
        filepath = os.path.abspath(filepath)
        if os.path.commonpath([attach_root, filepath]) == attach_root and os.path.isfile(filepath):
            os.remove(filepath)
        db.delete_page_attachment(attachment_id)
        wiki_logger.log_action("delete_attachment", request, user=user, filename=attachment["original_name"])
        notify_change("attachment_delete", f"Attachment '{attachment['original_name']}' deleted from page '{page['slug'] if page else 'unknown'}'")
        notify_file_deleted(attachment["filename"])
        return jsonify({"ok": True})

    @app.route("/page/<slug>/attachments/<int:attachment_id>/download")
    @login_required
    def download_attachment(slug, attachment_id):
        """Download a single attachment."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if not user_can_view_page(user, page):
            abort(403)
        if not db.has_permission(user, "attachment.view"):
            abort(403)
        attachment = db.get_page_attachment(attachment_id)
        if not attachment or attachment["page_id"] != page["id"]:
            abort(404)
        # Try DB blob first, then fall back to disk
        blob_id = attachment["blob_id"] if "blob_id" in attachment.keys() else None
        if blob_id:
            content = db.get_blob_content(blob_id)
            if content:
                return send_file(
                    io.BytesIO(content),
                    as_attachment=True,
                    download_name=attachment["original_name"],
                    mimetype=_ATTACHMENT_MIMETYPE,
                )
        # Fall back to disk
        attach_root = os.path.abspath(config.ATTACHMENT_FOLDER)
        filepath = os.path.abspath(os.path.join(attach_root, attachment["filename"]))
        if os.path.commonpath([attach_root, filepath]) != attach_root:
            abort(404)
        if not os.path.isfile(filepath):
            abort(404)
        return send_file(
            filepath,
            as_attachment=True,
            download_name=attachment["original_name"],
            mimetype=_ATTACHMENT_MIMETYPE,
        )

    @app.route("/page/<slug>/attachments/download-all")
    @login_required
    @rate_limit(10, 60, exempt_html_nav=False)
    def download_all_attachments(slug):
        """Download all attachments for a page as a ZIP file."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if not user_can_view_page(user, page):
            abort(403)
        if not db.has_permission(user, "attachment.view"):
            abort(403)
        attachments = db.get_page_attachments(page["id"])
        if not attachments:
            flash(t("flash.no_attachments_to_download"), "error")
            return redirect(url_for("view_page", slug=slug))
        attach_root = os.path.abspath(config.ATTACHMENT_FOLDER)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for att in attachments:
                content = None
                blob_id = att["blob_id"] if "blob_id" in att.keys() else None
                if blob_id:
                    content = db.get_blob_content(blob_id)
                if content is None:
                    filepath = os.path.abspath(os.path.join(attach_root, att["filename"]))
                    if os.path.commonpath([attach_root, filepath]) == attach_root and os.path.isfile(filepath):
                        with open(filepath, "rb") as f:
                            content = f.read()
                if content is not None:
                    zf.writestr(att["original_name"], content)
        buf.seek(0)
        zip_name = f"{slug}-attachments.zip"
        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=zip_name)

