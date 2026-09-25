"""Administration: appearance."""

from flask import request, redirect, url_for, flash, jsonify
import os, json, uuid
from PIL import Image
import db
from helpers import (
    login_required,
    admin_required,
    get_current_user,
    rate_limit,
    _safe_ext,
    t,
)
from wiki_logger import log_action
from sync import notify_file_upload, notify_file_deleted
from .admin_common import (
    favicon_upload_folder,
)


def register_admin_appearance_routes(app):
    """Register administration routes for appearance."""
    _VALID_FAVICON_TYPES = {
        "yellow",
        "green",
        "blue",
        "red",
        "orange",
        "cyan",
        "purple",
        "lime",
        "custom",
    }
    _FAVICON_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "ico", "gif", "webp"}

    def _allowed_favicon_file(filename):
        """Return True if *filename* has a permitted favicon extension."""
        return (
            "." in filename
            and filename.rsplit(".", 1)[1].lower() in _FAVICON_ALLOWED_EXTENSIONS
        )

    @app.route("/admin/settings/restore-favicon/<string:preset>", methods=["POST"])
    @app.route("/global-settings/restore-favicon/<string:preset>", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_restore_favicon(preset):
        """Restore a preset banana favicon to its default image."""
        valid_presets = _VALID_FAVICON_TYPES - {"custom"}
        if preset not in valid_presets:
            flash(t("flash.invalid_preset_type"), "error")
            return redirect(url_for("admin_settings"))

        from helpers._favicon import generate_banana_favicon, BANANA_FAVICON_COLORS

        if preset not in BANANA_FAVICON_COLORS:
            flash(t("flash.unknown_preset_type"), "error")
            return redirect(url_for("admin_settings"))

        img = generate_banana_favicon(preset)
        os.makedirs(favicon_upload_folder(), exist_ok=True)
        target_filename = f"banana_{preset}.png"
        filepath = os.path.join(favicon_upload_folder(), target_filename)
        try:
            img.save(filepath, format="PNG")
        except OSError:
            flash(t("flash.failed_to_restore_default_favicon"), "error")
            return redirect(url_for("admin_settings"))

        user = get_current_user()
        log_action("restore_favicon", request, user=user, preset=preset)
        flash(
            t("flash.default_preset_banana_icon_has_been_restored", preset=preset),
            "success",
        )
        return redirect(url_for("admin_settings"))

    @app.route("/admin/settings/favicon/select", methods=["POST"])
    @app.route("/global-settings/favicon/select", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(30, 60)
    def admin_favicon_select():
        """AJAX: Select a favicon as current by preset name or custom filename."""
        data = request.get_json(silent=True) or {}
        favicon_type = data.get("favicon_type", "").strip()
        favicon_custom = data.get("favicon_custom", "").strip()
        valid_presets = _VALID_FAVICON_TYPES - {"custom"}

        if favicon_type in valid_presets:
            db.update_site_settings(favicon_type=favicon_type, favicon_custom="")
            return jsonify({"ok": True, "type": favicon_type})
        elif (
            favicon_type == "custom"
            and favicon_custom
            and favicon_custom.startswith("custom_")
        ):
            fpath = os.path.join(favicon_upload_folder(), favicon_custom)
            if os.path.isfile(fpath):
                db.update_site_settings(
                    favicon_type="custom", favicon_custom=favicon_custom
                )
                return jsonify(
                    {"ok": True, "type": "custom", "filename": favicon_custom}
                )
        return jsonify({"ok": False, "error": "Invalid favicon selection"}), 400

    @app.route("/admin/settings/favicon/upload", methods=["POST"])
    @app.route("/global-settings/favicon/upload", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_favicon_upload():
        """AJAX: Upload a new custom favicon."""
        f = request.files.get("file")
        if not f or not f.filename or not _allowed_favicon_file(f.filename):
            return jsonify(
                {"ok": False, "error": t("flash.invalid_favicon_file_extension")}
            ), 400
        try:
            img = Image.open(f.stream)
            img.verify()
            f.stream.seek(0)
        except Exception:
            return jsonify(
                {"ok": False, "error": t("flash.custom_favicon_is_not_a_valid_image")}
            ), 400
        os.makedirs(favicon_upload_folder(), exist_ok=True)
        ext = _safe_ext(f.filename)
        if not ext:
            return jsonify(
                {"ok": False, "error": t("flash.invalid_favicon_file_extension")}
            ), 400
        filename = f"custom_{uuid.uuid4().hex}.{ext}"
        upload_root = os.path.abspath(favicon_upload_folder())
        filepath = os.path.abspath(os.path.join(upload_root, filename))
        if os.path.commonpath([upload_root, filepath]) != upload_root:
            return jsonify(
                {"ok": False, "error": t("flash.invalid_favicon_upload_path")}
            ), 400
        try:
            f.save(filepath)
        except OSError:
            return jsonify(
                {"ok": False, "error": t("flash.failed_to_save_custom_favicon")}
            ), 500
        notify_file_upload(filename, filepath, display_name="Custom favicon")
        # Add to favicon_order list
        current_order = json.loads(db.get_site_settings().get("favicon_order", "[]"))
        if filename not in current_order:
            current_order.append(filename)
            db.update_site_settings(favicon_order=json.dumps(current_order))
        # Auto-select the newly uploaded favicon
        db.update_site_settings(favicon_type="custom", favicon_custom=filename)
        return jsonify(
            {
                "ok": True,
                "filename": filename,
                "type": "custom",
                "selected": True,
            }
        )

    @app.route("/admin/settings/favicon/delete", methods=["POST"])
    @app.route("/global-settings/favicon/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_favicon_delete():
        """AJAX: Delete a custom favicon."""
        data = request.get_json(silent=True) or {}
        filename = data.get("filename", "").strip()
        if not filename or not filename.startswith("custom_"):
            return jsonify({"ok": False, "error": "Invalid favicon filename"}), 400
        fpath = os.path.join(favicon_upload_folder(), filename)
        if os.path.commonpath(
            [os.path.abspath(favicon_upload_folder()), os.path.abspath(fpath)]
        ) != os.path.abspath(favicon_upload_folder()):
            return jsonify(
                {"ok": False, "error": t("flash.invalid_favicon_upload_path")}
            ), 400
        if os.path.isfile(fpath):
            try:
                os.remove(fpath)
                notify_file_deleted(filename)
            except OSError:
                return jsonify({"ok": False, "error": "Failed to delete file"}), 500
        # Remove from favicon_order list
        settings = db.get_site_settings()
        current_order = json.loads(settings.get("favicon_order", "[]"))
        current_order = [f for f in current_order if f != filename]
        db.update_site_settings(favicon_order=json.dumps(current_order))
        # If this was the current favicon, fall back to default
        was_selected = (
            settings.get("favicon_custom") == filename
            or settings.get("favicon_type") == "custom"
            and settings.get("favicon_custom") == filename
        )
        if was_selected:
            db.update_site_settings(favicon_type="yellow", favicon_custom="")
        return jsonify(
            {
                "ok": True,
                "deleted": filename,
                "fallback_type": "yellow" if was_selected else None,
            }
        )

    @app.route("/admin/settings/favicon/reorder", methods=["POST"])
    @app.route("/global-settings/favicon/reorder", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(30, 60)
    def admin_favicon_reorder():
        """AJAX: Save new order of custom favicons."""
        data = request.get_json(silent=True) or {}
        order = data.get("order", [])
        if not isinstance(order, list):
            return jsonify({"ok": False, "error": "Invalid order data"}), 400
        # Validate all have valid filenames
        valid = [f for f in order if isinstance(f, str) and f.startswith("custom_")]
        # Verify files exist
        existing = []
        for fname in valid:
            fpath = os.path.join(favicon_upload_folder(), fname)
            if os.path.isfile(fpath):
                existing.append(fname)
        db.update_site_settings(favicon_order=json.dumps(existing))
        return jsonify({"ok": True})
