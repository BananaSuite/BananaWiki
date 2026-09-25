"""Administration: localization."""

from flask import render_template, request, redirect, url_for, flash, send_file
import io, json
import db
from helpers import (
    login_required,
    admin_required,
    get_current_user,
    rate_limit,
    BUILTIN_INTERFACE_LANGUAGES,
    normalize_docs_language,
    upsert_custom_interface_language,
    set_custom_interface_language_enabled,
    delete_custom_interface_language,
    t,
    list_translation_files,
    read_translation_file,
    write_translation_file,
    delete_translation_file,
    validate_language_payload,
    get_language_meta,
    MAX_TRANSLATION_FILE_BYTES,
)
from wiki_logger import log_action
from sync import notify_change
from .admin_common import (
    _safe_builtin_language_fallback,
)


def register_admin_localization_routes(app):
    """Register administration routes for localization."""

    @app.route("/admin/spawn-docs", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_spawn_docs():
        """Spawn (or respawn) the built-in BananaWiki documentation category."""
        user = get_current_user()
        simplified = request.form.get("simplified") == "1"
        settings = db.get_site_settings() or {}
        docs_language = normalize_docs_language(
            request.form.get("docs_language", settings.get("interface_language", "en")),
            settings,
            default="en",
        )
        db.spawn_wiki_docs(
            user["id"], simplified=simplified, language=docs_language
        )
        log_action(
            "spawn_docs",
            request,
            user=user,
            simplified=simplified,
            language=docs_language,
        )
        notify_change("spawn_docs", "Documentation category spawned")
        flash(t("flash.documentation_has_been_successfully_created"), "success")
        return redirect(url_for("admin_settings"))

    @app.route("/admin/download-docs", methods=["GET"])
    @login_required
    @admin_required
    @rate_limit(20, 60, exempt_html_nav=False)
    def admin_download_docs():
        """Download the spawn-able BananaWiki documentation as a ZIP archive.

        The archive layout mirrors the spawn function output (a top-level
        ``BananaWiki/`` folder with one Markdown file per page) and is
        directly re-importable through the existing **Bulk Markdown
        Import** flow, so admins can customise the docs locally and
        upload an edited bundle to ship a wiki-specific guide.
        """
        import io as _io

        user = get_current_user()
        simplified = request.args.get("simplified") == "1"
        settings = db.get_site_settings() or {}
        docs_language = normalize_docs_language(
            request.args.get("docs_language", settings.get("interface_language", "en")),
            settings,
            default="en",
        )
        zip_bytes, filename = db.build_docs_archive(
            simplified=simplified,
            language=docs_language,
        )
        log_action(
            "download_docs",
            request,
            user=user,
            simplified=simplified,
            language=docs_language,
        )
        return send_file(
            _io.BytesIO(zip_bytes),
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )

    @app.route("/admin/docs-settings", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_docs_settings():
        """Update the docs_bypass_deletion_slowdown flag."""
        if not db.is_plugin_enabled("deletion_slowdown"):
            flash(t("flash.the_deletion_slowdown_plugin_is_not_enabled"), "error")
            return redirect(url_for("admin_settings"))
        user = get_current_user()
        bypass = 1 if request.form.get("docs_bypass_deletion_slowdown") else 0
        db.update_site_settings(docs_bypass_deletion_slowdown=bypass)
        log_action("update_docs_settings", request, user=user, bypass=bypass)
        flash(t("flash.documentation_settings_have_been_saved"), "success")
        return redirect(url_for("admin_settings"))

    @app.route("/admin/interface-languages/add", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_add_interface_language():
        """Add or update a custom interface language entry."""
        user = get_current_user()
        settings = db.get_site_settings() or {}
        code = request.form.get("language_code", "").strip().lower()
        name = request.form.get("language_name", "").strip()
        enabled = bool(request.form.get("language_enabled"))
        try:
            serialized = upsert_custom_interface_language(
                settings, code, name, enabled=enabled
            )
        except ValueError:
            flash(t("flash.language_code_and_name_are_required_to_continue"), "error")
            return redirect(url_for("admin_settings"))
        db.update_site_settings(interface_languages_json=serialized)
        if settings.get("interface_language") == code and not enabled:
            db.update_site_settings(
                interface_language=_safe_builtin_language_fallback(
                    settings.get("interface_language_fallback", "en")
                )
            )
        log_action(
            "admin_interface_language_save",
            request,
            user=user,
            code=code,
            enabled=enabled,
        )
        notify_change("admin_interface_language_save", f"Language '{code}' saved")
        flash(t("flash.language_has_been_successfully_saved"), "success")
        return redirect(url_for("admin_settings"))

    @app.route("/admin/interface-languages/<string:code>/toggle", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(15, 60)
    def admin_toggle_interface_language(code):
        """Enable or disable a custom interface language."""
        user = get_current_user()
        settings = db.get_site_settings() or {}
        enabled = request.form.get("enabled", "1") == "1"
        try:
            serialized = set_custom_interface_language_enabled(settings, code, enabled)
        except ValueError:
            flash(t("flash.language_could_not_be_updated"), "error")
            return redirect(url_for("admin_settings"))
        db.update_site_settings(interface_languages_json=serialized)
        if not enabled and settings.get("interface_language") == code:
            db.update_site_settings(
                interface_language=_safe_builtin_language_fallback(
                    settings.get("interface_language_fallback", "en")
                )
            )
        log_action(
            "admin_interface_language_toggle",
            request,
            user=user,
            code=code,
            enabled=enabled,
        )
        notify_change("admin_interface_language_toggle", f"Language '{code}' toggled")
        flash(t("flash.language_has_been_successfully_updated"), "success")
        return redirect(url_for("admin_settings"))

    @app.route("/admin/interface-languages/<string:code>/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_delete_interface_language(code):
        """Delete a custom interface language entry."""
        user = get_current_user()
        settings = db.get_site_settings() or {}
        if code in BUILTIN_INTERFACE_LANGUAGES:
            flash(t("flash.builtin_languages_cannot_be_deleted"), "error")
            return redirect(url_for("admin_settings"))
        try:
            serialized = delete_custom_interface_language(settings, code)
        except ValueError:
            flash(t("flash.language_could_not_be_deleted"), "error")
            return redirect(url_for("admin_settings"))
        update_kwargs = {"interface_languages_json": serialized}
        if settings.get("interface_language") == code:
            update_kwargs["interface_language"] = _safe_builtin_language_fallback(
                settings.get("interface_language_fallback", "en")
            )
        db.update_site_settings(**update_kwargs)
        log_action("admin_interface_language_delete", request, user=user, code=code)
        notify_change("admin_interface_language_delete", f"Language '{code}' deleted")
        flash(t("flash.language_has_been_successfully_deleted"), "success")
        return redirect(url_for("admin_settings"))

    @app.route("/admin/interface-languages")
    @login_required
    @admin_required
    def admin_languages():
        """Render the dedicated language manager page."""
        user = get_current_user()
        settings = db.get_site_settings() or {}
        installed_codes = set(list_translation_files())
        registered = []
        from helpers import get_all_interface_languages

        for entry in get_all_interface_languages(settings):
            code = entry.get("code")
            meta = get_language_meta(code) if code else {}
            registered.append(
                {
                    "code": code,
                    "name": entry.get("name") or meta.get("name") or code,
                    "english_name": meta.get("english_name")
                    or meta.get("name")
                    or code,
                    "enabled": entry.get("enabled", True),
                    "builtin": entry.get("builtin", False),
                    "rtl": bool(meta.get("rtl", False)),
                    "author": meta.get("author", ""),
                    "based_on_version": meta.get("based_on_version", ""),
                    "has_file": code in installed_codes,
                }
            )
        registered_codes = {r["code"] for r in registered}
        orphan = [c for c in installed_codes if c not in registered_codes]
        log_action("view_admin_languages", request, user=user)
        return render_template(
            "admin/languages.html",
            languages=registered,
            orphan_files=orphan,
            site_default_language=settings.get("interface_language", "en"),
        )

    @app.route("/admin/interface-languages/<string:code>/download")
    @login_required
    @admin_required
    def admin_download_language(code):
        """Download a language pack JSON (built-in or custom)."""
        data = read_translation_file(code)
        if data is None:
            flash(t("flash.language_pack_file_not_found"), "error")
            return redirect(url_for("admin_languages"))
        # Ensure a sensible _meta block is present in the download.
        if "_meta" not in data or not isinstance(data.get("_meta"), dict):
            data = {"_meta": get_language_meta(code), **data}
        log_action(
            "admin_download_language", request, user=get_current_user(), code=code
        )
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        return send_file(
            io.BytesIO(payload.encode("utf-8")),
            mimetype="application/json",
            as_attachment=True,
            download_name=f"{code}.json",
        )

    @app.route("/admin/interface-languages/upload", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_upload_language():
        """Upload (create or replace) a language pack JSON file."""
        user = get_current_user()
        upload = request.files.get("language_file")
        if not upload or not upload.filename:
            flash(t("flash.language_file_is_required_to_continue"), "error")
            return redirect(url_for("admin_languages"))
        # Read and size-check before parsing JSON.
        raw = upload.read(MAX_TRANSLATION_FILE_BYTES + 1)
        if len(raw) > MAX_TRANSLATION_FILE_BYTES:
            flash(t("flash.language_file_is_too_large"), "error")
            return redirect(url_for("admin_languages"))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            flash(t("flash.language_file_is_not_valid_json"), "error")
            return redirect(url_for("admin_languages"))
        normalized, error = validate_language_payload(payload)
        if error or normalized is None:
            flash(error or t("flash.language_file_is_invalid"), "error")
            return redirect(url_for("admin_languages"))
        code = normalized["_meta"]["code"]
        ok, write_error = write_translation_file(code, normalized)
        if not ok:
            flash(write_error or t("flash.language_could_not_be_saved"), "error")
            return redirect(url_for("admin_languages"))
        # Register the language in site settings (custom only).
        settings = db.get_site_settings() or {}
        if code not in BUILTIN_INTERFACE_LANGUAGES:
            try:
                serialized = upsert_custom_interface_language(
                    settings,
                    code,
                    normalized["_meta"].get("name") or code,
                    enabled=True,
                )
                db.update_site_settings(interface_languages_json=serialized)
            except ValueError:
                pass
        log_action("admin_upload_language", request, user=user, code=code)
        notify_change("admin_upload_language", f"Language '{code}' uploaded")
        flash(t("flash.language_has_been_successfully_uploaded"), "success")
        return redirect(url_for("admin_languages"))

    @app.route("/admin/interface-languages/<string:code>/delete-file", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_delete_language_file(code):
        """Delete a custom language pack JSON file from disk."""
        user = get_current_user()
        # Guard built-ins; the metadata DELETE route already covers built-ins.
        if code in BUILTIN_INTERFACE_LANGUAGES:
            flash(t("flash.builtin_languages_cannot_be_deleted"), "error")
            return redirect(url_for("admin_languages"))
        ok, error = delete_translation_file(code)
        if not ok:
            flash(error or t("flash.language_could_not_be_deleted"), "error")
            return redirect(url_for("admin_languages"))
        # Drop the metadata too, if it exists.
        settings = db.get_site_settings() or {}
        try:
            serialized = delete_custom_interface_language(settings, code)
            update_kwargs = {"interface_languages_json": serialized}
            if settings.get("interface_language") == code:
                update_kwargs["interface_language"] = _safe_builtin_language_fallback(
                    settings.get("interface_language_fallback", "en")
                )
            db.update_site_settings(**update_kwargs)
        except ValueError:
            pass
        log_action("admin_delete_language_file", request, user=user, code=code)
        notify_change("admin_delete_language_file", f"Language '{code}' deleted")
        flash(t("flash.language_has_been_successfully_deleted"), "success")
        return redirect(url_for("admin_languages"))
