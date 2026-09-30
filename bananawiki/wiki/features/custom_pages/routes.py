"""Custom pages: administration, file downloads, sandboxed documents and the catch-all."""

from __future__ import annotations

import logging

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.routing import Map

from ....core.web import csrf_exempt
from ... import auth, registry, settings, storage
from ...i18n import t
from . import rendering, service

log = logging.getLogger("bananawiki.custom_pages")

bp = registry.feature_blueprint(
    "custom_pages", "custom_pages", __name__,
    template_folder="templates", static_folder="static", static_url_path="/static/custom_pages",
)

MAX_VIDEO_MB = 10_240
SERVED_METHODS = ("GET", "HEAD")
NEW_PAGE = {
    "content_type": "wiki_page", "is_published": 1, "redirect_code": 302, "links_json": "[]",
    "code_language": "text", "iframe_height": service.DEFAULT_IFRAME_HEIGHT, "video_controls": 1,
}


def _require_manager() -> None:
    if not service.can_manage():
        abort(403)


def _page_or_404(page_id: int) -> dict:
    page = service.get(page_id)
    if page is None:
        abort(404)
    return page


def _store_uploads(page: dict) -> None:
    for upload in request.files.getlist("file"):
        if not upload or not upload.filename:
            continue
        try:
            service.add_file(page, upload)
        except storage.UploadError as error:
            flash(t("custom_pages.flash.upload_failed", name=upload.filename, reason=t(error.key, **error.values)),
                  "error")


def _form_context(page: dict | None, form=None) -> dict:
    if form is None:
        form = {**page, "content_type": service.form_type(page)} if page else NEW_PAGE
    return {
        "page": page,
        "form": form,
        "is_builder": bool(page) and service.is_builder_page(page),
        "builder_available": service.builder_available(),
        "files": service.files(page["id"]) if page else [],
        "groups": service.CONTENT_TYPE_GROUPS,
        "type_fields": {kind: sorted(fields) for kind, fields in service.TYPE_FIELDS.items()},
        "max_video_mb": service.max_video_bytes() // (1024 * 1024),
    }


# ── Administration ────────────────────────────────────────────────────────────


@bp.get("/admin/custom-pages")
def admin_list():
    _require_manager()
    return render_template("custom_pages/admin_list.html", pages=service.list_all(),
                           groups=service.CONTENT_TYPE_GROUPS, builder_available=service.builder_available(),
                           max_video_mb=service.max_video_bytes() // (1024 * 1024))


@bp.post("/admin/custom-pages/settings")
def admin_settings():
    _require_manager()
    try:
        size = int(request.form.get("custom_pages_max_video_size_mb", ""))
    except ValueError:
        size = 0
    if not 1 <= size <= MAX_VIDEO_MB:
        auth.flash_t("custom_pages.error.video_size", "error", maximum=MAX_VIDEO_MB)
    else:
        settings.update({"custom_pages_max_video_size_mb": size})
        auth.flash_t("common.saved", "success")
    return redirect(url_for("custom_pages.admin_list"))


@bp.route("/admin/custom-pages/create", methods=["GET", "POST"])
def admin_create():
    _require_manager()
    if request.method == "POST":
        try:
            values = service.clean(request.form)
            page_id = service.create(values, created_by=auth.current_user()["id"])
        except service.CustomPageError as error:
            flash(t(error.key, **error.values), "error")
            return render_template("custom_pages/admin_edit.html", **_form_context(None, request.form)), 400
        page = service.get(page_id)
        _store_uploads(page)
        log.info("Custom page %s created by %s", values["path"], auth.current_user()["username"])
        auth.flash_t("custom_pages.flash.created", "success")
        if service.is_builder_page(page):
            return redirect(url_for("page_builder.custom_editor", page_id=page_id))
        return redirect(url_for("custom_pages.admin_edit", page_id=page_id))
    return render_template("custom_pages/admin_edit.html", **_form_context(None))


@bp.route("/admin/custom-pages/<int:page_id>/edit", methods=["GET", "POST"])
def admin_edit(page_id: int):
    _require_manager()
    page = _page_or_404(page_id)
    if request.method == "POST":
        try:
            values = service.clean(request.form, page_id=page_id, current=page)
            service.update(page_id, values)
        except service.CustomPageError as error:
            flash(t(error.key, **error.values), "error")
            return render_template("custom_pages/admin_edit.html", **_form_context(page, request.form)), 400
        _store_uploads(service.get(page_id))
        log.info("Custom page %s updated by %s", values["path"], auth.current_user()["username"])
        auth.flash_t("custom_pages.flash.updated", "success")
        return redirect(url_for("custom_pages.admin_edit", page_id=page_id))
    return render_template("custom_pages/admin_edit.html", **_form_context(page))


@bp.post("/admin/custom-pages/<int:page_id>/delete")
def admin_delete(page_id: int):
    _require_manager()
    page = _page_or_404(page_id)
    service.delete(page)
    log.info("Custom page %s deleted by %s", page["path"], auth.current_user()["username"])
    auth.flash_t("custom_pages.flash.deleted", "success")
    return redirect(url_for("custom_pages.admin_list"))


@bp.post("/admin/custom-pages/files/<int:file_id>/delete")
def admin_file_delete(file_id: int):
    _require_manager()
    row = service.get_file(file_id)
    if row is None:
        abort(404)
    service.delete_file(row)
    auth.flash_t("custom_pages.flash.file_deleted", "success")
    return redirect(url_for("custom_pages.admin_edit", page_id=row["custom_page_id"]))


# ── Public serving (published pages are public on purpose) ─────────────────────


@bp.get("/_cpf/<int:file_id>/<path:filename>")
@auth.public
def file_download(file_id: int, filename: str):
    """A file of a custom page (the 1.4 URL shape). *filename* is cosmetic."""
    row = service.get_file(file_id)
    if row is None or not service.is_visible(service.get(row["custom_page_id"])):
        abort(404)
    return rendering.send_file_row(row)


@bp.get("/_cpd/<int:page_id>")
@auth.public
def document(page_id: int):
    """The author's HTML document, only ever served with a CSP sandbox."""
    page = service.get(page_id)
    if not service.is_visible(page) or page["content_type"] not in service.SANDBOXED_TYPES:
        abort(404)
    return rendering.document_response(page)


def _other_routes() -> Map:
    """The URL map without the catch-all, to tell 404 from 405 and slash redirects."""
    rules = list(current_app.url_map.iter_rules())
    cached = current_app.extensions.get("custom_pages.map")
    if cached is not None and cached[0] == len(rules):
        return cached[1]
    url_map = current_app.url_map
    shadow = Map([rule.empty() for rule in rules if rule.endpoint != service.CATCH_ALL_ENDPOINT],
                 converters=dict(url_map.converters), strict_slashes=url_map.strict_slashes,
                 merge_slashes=url_map.merge_slashes)
    current_app.extensions["custom_pages.map"] = (len(rules), shadow)
    return shadow


def _no_custom_page():
    """Answer as if the catch-all did not exist (404, 405 or a slash redirect)."""
    _other_routes().bind_to_environ(request.environ).match(method=request.method)
    raise NotFound()


@auth.public
@csrf_exempt
def serve(path: str):
    """Serve the custom page at ``request.path`` when no other route claims it."""
    if not registry.is_enabled("custom_pages"):
        return _no_custom_page()
    page = service.get_by_path(request.path)
    if page is None and request.path.endswith("/") and len(request.path) > 1:
        trimmed = service.get_by_path(request.path.rstrip("/"))
        if service.is_visible(trimmed) and request.method in SERVED_METHODS:
            return redirect(trimmed["path"], code=308)
    if not service.is_visible(page):
        return _no_custom_page()
    if request.method not in SERVED_METHODS:
        raise MethodNotAllowed(valid_methods=list(SERVED_METHODS))
    return rendering.serve(page)


def register_catch_all(app) -> None:
    """Add ``/<path:path>``; Werkzeug only picks it when no other rule matches."""
    app.add_url_rule("/<path:path>", endpoint=service.CATCH_ALL_ENDPOINT, view_func=serve,
                     methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
