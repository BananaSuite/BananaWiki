"""Export routes (1.4 URLs): page PDF and Markdown, Markdown into the editor, bulk Markdown."""

from __future__ import annotations

import io
from typing import Any

from flask import Blueprint, abort, jsonify, redirect, render_template, request, send_file, url_for

from ....core.timeutil import utcnow
from ... import auth, registry, settings
from ...i18n import t
from ...templating import format_datetime
from ..audit import record
from ..pages import categories
from ..pages import service as pages
from ..pages.blueprint import rate_limited, visible_page_or_404
from ..pages.rendering import editor_name
from . import bulk, pdf
from . import markdown_files as mdf

bp = Blueprint("page_export", __name__, template_folder="templates", static_folder="static",
               static_url_path="/static/page_export")


# ── Who may export ────────────────────────────────────────────────────────────


def pdf_allowed(user: dict[str, Any] | None = None) -> bool:
    user = auth.current_user() if user is None else user
    return bool(user) and bool(settings.get("pdf_export_enabled")) and auth.has_permission("page.export_pdf", user)


def markdown_allowed(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    """Markdown source downloads are for the page's editors, as in 1.4."""
    user = auth.current_user() if user is None else user
    return bool(user) and bool(settings.get("markdown_export_enabled")) and pages.can_edit(page, user)


def _pdf_response(page: dict[str, Any], *, title: str, content: str, author_id: str | None, when: str | None,
                  name: str, historical: bool = False):
    meta = []
    author = editor_name(author_id)
    if author:
        meta.append(t("page_export.pdf.by", user=author))
    if when:
        meta.append(t("page_export.pdf.edited", when=format_datetime(when)))
    if historical:
        meta.append(t("page_export.pdf.historical"))
    data = pdf.render(title=title, content=content, site_name=settings.site_name(), base_url=request.url_root,
                      meta=meta)
    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=name,
                     max_age=0)


def _require_pdf() -> None:
    if not settings.get("pdf_export_enabled"):
        abort(404)
    if not pdf_allowed():
        abort(403)


# ── Single page ──────────────────────────────────────────────────────────────


@bp.get("/page/<slug>/export-pdf")
@rate_limited("export-pdf", 5)
def export_pdf(slug: str):
    page = visible_page_or_404(slug)
    _require_pdf()
    return _pdf_response(page, title=page["title"], content=page["content"] or "",
                         author_id=page.get("last_edited_by"), when=page.get("last_edited_at"),
                         name=f"{page['slug']}.pdf")


@bp.get("/page/<slug>/history/<int:entry_id>/export-pdf")
@rate_limited("export-pdf", 5)
def export_history_pdf(slug: str, entry_id: int):
    page = visible_page_or_404(slug)
    if not registry.is_enabled("page_history"):
        abort(404)
    _require_pdf()
    if not auth.has_permission("history.view"):
        abort(403)
    entry = pages.history_entry(entry_id)
    if entry is None or entry["page_id"] != page["id"]:
        abort(404)
    return _pdf_response(page, title=entry["title"], content=entry["content"] or "", author_id=entry["edited_by"],
                         when=entry["created_at"], name=f"{page['slug']}-revision-{entry_id}.pdf", historical=True)


@bp.get("/page/<slug>/export-md")
@rate_limited("export-md", 20)
def export_markdown(slug: str):
    page = visible_page_or_404(slug)
    if not settings.get("markdown_export_enabled"):
        abort(404)
    if not markdown_allowed(page):
        abort(403)
    category = categories.get(page["category_id"]) if page["category_id"] else None
    text = mdf.document({"title": page["title"], "slug": page["slug"],
                         "category": category["name"] if category else "",
                         "exported_at": page.get("last_edited_at") or ""}, page["content"] or "")
    return send_file(io.BytesIO(text.encode("utf-8")), mimetype="text/markdown; charset=utf-8", as_attachment=True,
                     download_name=f"{page['slug']}.md", max_age=0)


@bp.post("/page/<slug>/import-md")
@rate_limited("import-md", 20)
def import_markdown(slug: str):
    """Read an uploaded Markdown file for the editor; the page itself is not changed."""
    page = visible_page_or_404(slug)
    if not pages.can_edit(page):
        return jsonify({"error": t("error.forbidden")}), 403
    upload = request.files.get("import_file")
    if upload is None or not upload.filename:
        return jsonify({"error": t("upload.error.no_file")}), 400
    raw = upload.stream.read(bulk.MAX_FILE_BYTES + 1)
    parsed = mdf.parse(mdf.decode(raw))
    if len(raw) > bulk.MAX_FILE_BYTES or len(parsed.body) > pages.MAX_CONTENT:
        return jsonify({"error": t("page_export.import.content_too_large")}), 400
    title = parsed.meta.get("title") or None
    return jsonify({"content": parsed.body, "title": title[:pages.MAX_TITLE] if title else None})


# ── Bulk Markdown (administrators) ───────────────────────────────────────────


@bp.get("/admin/bulk-markdown")
@auth.admin_required
def bulk_page():
    return render_template("page_export/bulk_markdown.html", modes=bulk.MODES, max_files=bulk.MAX_FILES)


@bp.post("/admin/bulk-markdown/export")
@auth.admin_required
@rate_limited("bulk-markdown", 5)
def bulk_export():
    archive, summary = bulk.export_archive()
    record("page_export.bulk_export", details={"pages": summary.pages, "uploads": summary.uploads,
                                               "attachments": summary.attachments,
                                               "missing": len(summary.missing)})
    name = f"markdown_export_{utcnow():%Y%m%d_%H%M%S}.zip"
    return send_file(archive, mimetype="application/zip", as_attachment=True, download_name=name, max_age=0)


def _flash_import(result: bulk.ImportResult) -> None:
    for key, values in result.problems[:20]:
        extra = {"reason": t(values["reason_key"], **values)} if "reason_key" in values else {}
        auth.flash_t(key, "warning", **{**values, **extra})
    if len(result.problems) > 20:
        auth.flash_t("page_export.import.more_problems", "warning", count=len(result.problems) - 20)
    if result.created or result.updated:
        auth.flash_t("page_export.import.done", "success", created=len(result.created), updated=len(result.updated),
                     skipped=len(result.skipped), categories=result.categories)
    elif not result.problems:
        auth.flash_t("page_export.import.nothing", "info", skipped=len(result.skipped))


@bp.post("/admin/bulk-markdown")
@auth.admin_required
@rate_limited("bulk-markdown", 5)
def bulk_import():
    uploads = [f for f in request.files.getlist("import_file") if f and f.filename]
    if not uploads:
        auth.flash_t("upload.error.no_file", "error")
        return redirect(url_for("page_export.bulk_page"))
    mode = request.form.get("mode", "skip")
    mode = mode if mode in bulk.MODES else "skip"
    user = auth.current_user()
    author_id = user["id"] if request.form.get("attribute_to_me") == "1" else None
    result = bulk.ImportResult()
    try:
        files = bulk.read_uploads(uploads, result)
    except bulk.ImportRefused as refused:
        auth.flash_t(refused.key, "error", **refused.values)
        return redirect(url_for("page_export.bulk_page"))
    bulk.import_files(files, author_id=author_id, actor_id=user["id"], mode=mode, result=result)
    record("page_export.bulk_import", details={
        "files": len(files), "created": len(result.created), "updated": len(result.updated),
        "skipped": len(result.skipped), "categories": result.categories, "problems": len(result.problems),
        "mode": mode, "attributed_to": "user" if author_id else "system",
    })
    _flash_import(result)
    return redirect(url_for("page_export.bulk_page"))
