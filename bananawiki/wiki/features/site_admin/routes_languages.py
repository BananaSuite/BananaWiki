"""Interface language views (1.4 URLs under ``/admin/interface-languages``)."""

from __future__ import annotations

import io
import json

from flask import abort, redirect, render_template, request, send_file, url_for

from ... import auth, settings
from ...i18n import BUILTIN_LANGUAGES
from ..audit import record
from . import languages
from .blueprint import bp
from .languages import LanguageError


def _back():
    return redirect(url_for("site_admin.languages_page"))


def _refuse(error: LanguageError):
    auth.flash_t(error.key, "error", **error.values)
    return _back()


@bp.get("/admin/interface-languages")
@auth.admin_required
def languages_page():
    rows = languages.overview()
    return render_template(
        "site_admin/languages.html", languages=rows, default_language=languages.default_language(),
        fallback=settings.get("interface_language_fallback") or "en", builtins=BUILTIN_LANGUAGES,
        max_mb=languages.MAX_FILE_BYTES // (1024 * 1024),
    )


@bp.post("/admin/interface-languages/default")
@auth.admin_required
def set_default_language():
    code = request.form.get("interface_language", "")
    try:
        languages.set_default(code, request.form.get("interface_language_fallback", "en"))
    except LanguageError as error:
        return _refuse(error)
    record("language.default_changed", target_type="language", target_id=code)
    auth.flash_t("common.saved", "success")
    return _back()


@bp.post("/admin/interface-languages/<string:code>/toggle")
@auth.admin_required
def toggle_language(code: str):
    enabled = request.form.get("enabled", "1") == "1"
    try:
        languages.set_enabled(code, enabled)
    except LanguageError as error:
        return _refuse(error)
    record("language.toggled", target_type="language", target_id=code, details={"enabled": enabled})
    auth.flash_t("site_admin.languages.enabled_done" if enabled else "site_admin.languages.disabled_done",
                 "success", code=code)
    return _back()


@bp.post("/admin/interface-languages/add")
@auth.admin_required
def add_language():
    """1.4 registered a language by name; 1.6 needs its file, so this only enables installed ones."""
    code = request.form.get("language_code", "")
    try:
        languages.set_enabled(code, bool(request.form.get("language_enabled", "1")))
    except LanguageError:
        return _refuse(LanguageError("site_admin.languages.error.needs_file"))
    record("language.toggled", target_type="language", target_id=code)
    auth.flash_t("common.saved", "success")
    return _back()


@bp.post("/admin/interface-languages/upload")
@auth.admin_required
def upload_language():
    try:
        code, ignored = languages.install(languages.read_upload(request.files.get("language_file")))
    except LanguageError as error:
        return _refuse(error)
    record("language.uploaded", target_type="language", target_id=code, details={"ignored_keys": ignored})
    auth.flash_t("site_admin.languages.uploaded", "success", code=code)
    if ignored:
        auth.flash_t("site_admin.languages.ignored_keys", "warning", count=ignored)
    return _back()


@bp.get("/admin/interface-languages/<string:code>/download")
@auth.admin_required
def download_language(code: str):
    data = languages.download(code)
    if data is None:
        abort(404)
    body = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return send_file(io.BytesIO(body), mimetype="application/json", as_attachment=True,
                     download_name=f"{data['_meta']['code']}.json")


@bp.post("/admin/interface-languages/<string:code>/delete-file")
@bp.post("/admin/interface-languages/<string:code>/delete", endpoint="delete_language_legacy")
@auth.admin_required
def delete_language(code: str):
    try:
        languages.delete_file(code)
    except LanguageError as error:
        return _refuse(error)
    record("language.deleted", target_type="language", target_id=code)
    auth.flash_t("site_admin.languages.deleted", "success", code=code)
    return _back()
