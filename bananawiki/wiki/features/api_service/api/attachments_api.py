"""Page attachments: list, download, upload and delete (``pages`` scope, Attachments feature).

The rules are the attachment panel's (``features/attachments``): listing and
downloading need read access to the page and ``attachment.view``; uploading
needs edit rights on an unprotected page and ``attachment.upload``;
deleting needs ``attachment.delete_any`` or, for one's own files,
``attachment.delete_own``. Uploads count towards the account's daily upload
quota, like uploads in the browser.
"""

from __future__ import annotations

from typing import Any

from flask import request

from .... import storage
from ...attachments import service as attachments
from ...audit import record
from .. import serialize
from ..errors import ApiError
from . import bp, caller, ok, requires
from .pages import readable

FEATURE = "attachments"


def upload_error(error: storage.UploadError) -> ApiError:
    status = 413 if error.key == "upload.error.too_large" else 429 if "quota" in error.key else 400
    return ApiError(status, "upload_refused", error.key, **error.values)


def send_stored(folder: str, row: dict[str, Any]):
    """The stored file as a download, or 404 when it is gone from disk."""
    if storage.resolve(folder, row["filename"], blob_id=row.get("blob_id")) is None:
        raise ApiError(404, "file_missing")
    return storage.send(folder, row["filename"], download_name=row["original_name"], blob_id=row.get("blob_id"),
                        max_age=0)


def _downloadable(slug: str) -> dict[str, Any]:
    page = readable(slug)
    if not attachments.can_download(page, caller()):
        raise ApiError(403, "cannot_view_attachments")
    return page


def _attachment(page: dict[str, Any], attachment_id: int) -> dict[str, Any]:
    row = attachments.get(attachment_id)
    if row is None or row["page_id"] != page["id"]:
        raise ApiError(404, "attachment_not_found")
    return row


@bp.get("/pages/<slug>/attachments")
@requires("pages", feature=FEATURE)
def list_page_attachments(slug: str):
    page = _downloadable(slug)
    return ok(attachments=[serialize.attachment(row) for row in attachments.for_page(page["id"])])


@bp.get("/pages/<slug>/attachments/<int:attachment_id>")
@requires("pages", feature=FEATURE)
def download_page_attachment(slug: str, attachment_id: int):
    page = _downloadable(slug)
    return send_stored(attachments.FOLDER, _attachment(page, attachment_id))


@bp.post("/pages/<slug>/attachments")
@requires("pages", write=True, feature=FEATURE)
def upload_page_attachment(slug: str):
    """``multipart/form-data`` with the file in the ``file`` field."""
    page = readable(slug)
    if not attachments.can_upload(page, caller()):
        raise ApiError(403, "cannot_upload")
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ApiError(400, "file_required")
    try:
        row = attachments.add(page, upload, caller())
    except storage.UploadError as error:
        raise upload_error(error) from None
    record("attachment.uploaded", target_type="page", target_id=page["id"],
           details={"name": row["original_name"], "size": row["file_size"], "via": "api"})
    return ok(201, attachment=serialize.attachment({**row, "uploader": caller()["username"]}))


@bp.delete("/pages/<slug>/attachments/<int:attachment_id>")
@requires("pages", write=True, feature=FEATURE)
def delete_page_attachment(slug: str, attachment_id: int):
    page = readable(slug)
    row = _attachment(page, attachment_id)
    if not attachments.can_delete(row, page, caller()):
        raise ApiError(403, "cannot_delete_attachment")
    attachments.delete(row)
    record("attachment.deleted", target_type="page", target_id=page["id"],
           details={"name": row["original_name"], "via": "api"})
    return ok(deleted=True, id=attachment_id)
