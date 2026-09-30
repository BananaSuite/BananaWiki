"""Attachment routes (1.4 URLs).

HTML forms on the page view post to ``/page/<slug>/attachments…``; the
editor's script uses the JSON endpoints ``/api/page/<id>/attachments`` and
``DELETE /api/attachments/<id>``.
"""

from __future__ import annotations

from typing import Any

from flask import abort, jsonify, redirect, request, send_file, url_for

from ... import auth, storage
from ...i18n import t
from ...registry import feature_blueprint
from ..audit import record
from ..pages import service as pages
from ..pages.blueprint import page_url, rate_limited, visible_page_or_404
from . import service

bp = feature_blueprint("attachments", "attachments", __name__, template_folder="templates",
                       static_folder="static", static_url_path="/static/attachments")

MAX_FILES_PER_REQUEST = 20


def _json_error(key: str, status: int, **values: Any):
    return jsonify({"error": t(key, **values)}), status


def serialize(attachment: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": attachment["id"],
        "name": attachment["original_name"],
        "size": attachment["file_size"],
        "download_url": url_for("attachments.download", slug=page["slug"], attachment_id=attachment["id"]),
        "delete_url": url_for("attachments.api_delete", attachment_id=attachment["id"]),
    }


def _attachment_of(page: dict[str, Any], attachment_id: int) -> dict[str, Any]:
    attachment = service.get(attachment_id)
    if attachment is None or attachment["page_id"] != page["id"]:
        abort(404)
    return attachment


def _uploaded(page: dict[str, Any], upload) -> dict[str, Any]:
    attachment = service.add(page, upload, auth.current_user())
    record("attachment.uploaded", target_type="page", target_id=page["id"],
           details={"name": attachment["original_name"], "size": attachment["file_size"]})
    return attachment


def _deleted(page: dict[str, Any], attachment: dict[str, Any]) -> None:
    service.delete(attachment)
    record("attachment.deleted", target_type="page", target_id=page["id"],
           details={"name": attachment["original_name"]})


# ── Downloads ────────────────────────────────────────────────────────────────


@bp.get("/page/<slug>/attachments/<int:attachment_id>/download")
@rate_limited("attachment-download", 120)
def download(slug: str, attachment_id: int):
    page = visible_page_or_404(slug)
    if not service.can_download(page):
        abort(403)
    attachment = _attachment_of(page, attachment_id)
    return storage.send(service.FOLDER, attachment["filename"], download_name=attachment["original_name"],
                        blob_id=attachment.get("blob_id"), max_age=0)


@bp.get("/page/<slug>/attachments/download-all")
@rate_limited("attachment-zip", 10)
def download_all(slug: str):
    page = visible_page_or_404(slug)
    if not service.can_download(page):
        abort(403)
    attachments = service.for_page(page["id"])
    if not attachments:
        auth.flash_t("attachments.flash.none_to_download", "error")
        return redirect(page_url(page))
    archive = service.zip_archive(attachments)
    return send_file(archive, mimetype="application/zip", as_attachment=True,
                     download_name=f"{page['slug']}-attachments.zip", max_age=0)


# ── Upload ───────────────────────────────────────────────────────────────────


@bp.post("/page/<slug>/attachments")
@rate_limited("attachment-upload", 20)
def upload(slug: str):
    page = visible_page_or_404(slug)
    if not service.can_upload(page):
        abort(403)
    files = [f for f in request.files.getlist("files") if f and f.filename][:MAX_FILES_PER_REQUEST]
    if not files:
        auth.flash_t("upload.error.no_file", "error")
        return redirect(page_url(page))
    added = 0
    for upload_file in files:
        try:
            _uploaded(page, upload_file)
            added += 1
        except storage.UploadError as error:
            auth.flash_t("attachments.flash.refused", "error", name=upload_file.filename or "",
                         reason=t(error.key, **error.values))
    if added:
        auth.flash_t("attachments.flash.uploaded", "success", count=added)
    return redirect(page_url(page) + "#attachments")


@bp.post("/api/page/<int:page_id>/attachments")
@rate_limited("attachment-upload", 20)
def api_upload(page_id: int):
    page = pages.get(page_id, with_content=False)
    if page is None or not pages.can_view(page):
        return _json_error("attachments.error.page_missing", 404)
    if not service.can_upload(page):
        return _json_error("error.forbidden", 403)
    try:
        attachment = _uploaded(page, request.files.get("file"))
    except storage.UploadError as error:
        status = 413 if error.key == "upload.error.too_large" else 429 if "quota" in error.key else 400
        return _json_error(error.key, status, **error.values)
    return jsonify(serialize(attachment, page)), 201


# ── Delete ───────────────────────────────────────────────────────────────────


@bp.post("/page/<slug>/attachments/<int:attachment_id>/delete")
@rate_limited("attachment-delete", 20)
def delete(slug: str, attachment_id: int):
    page = visible_page_or_404(slug)
    attachment = _attachment_of(page, attachment_id)
    if not service.can_delete(attachment, page):
        abort(403)
    _deleted(page, attachment)
    auth.flash_t("attachments.flash.deleted", "success", name=attachment["original_name"])
    return redirect(page_url(page) + "#attachments")


@bp.delete("/api/attachments/<int:attachment_id>")
@rate_limited("attachment-delete", 20)
def api_delete(attachment_id: int):
    attachment = service.get(attachment_id)
    page = pages.get(attachment["page_id"], with_content=False) if attachment else None
    if attachment is None or page is None or not pages.can_view(page):
        return _json_error("attachments.error.missing", 404)
    if not service.can_delete(attachment, page):
        return _json_error("error.forbidden", 403)
    _deleted(page, attachment)
    return jsonify({"ok": True})
