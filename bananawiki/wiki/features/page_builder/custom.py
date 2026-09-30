"""The visual builder for custom pages (administrators holding ``custom_page.manage``).

A custom page built here is stored by the custom pages feature
(:func:`custom_pages.service.save_builder_document`) and rendered with the
same renderer as builder wiki pages. Custom pages have no drafts: the editor
keeps changes in the browser until they are saved, and a save is refused
when the page changed since the editor was opened.
"""

from __future__ import annotations

import hashlib
from typing import Any

from flask import abort, jsonify, render_template, url_for

from ... import auth, registry
from ..custom_pages import service as custom_pages
from . import document
from .routes import (
    PREVIEWS_PER_MINUTE,
    Refused,
    bp,
    common_urls,
    editor_options,
    json_payload,
    preview_response,
    rate_limit,
    store_image,
    validated_document,
)


def version_token(page: dict[str, Any]) -> str:
    """Changes whenever anything the editor shows changes (the form or another editor saved)."""
    parts = (page["updated_at"], page["title"], page["content_type"], page["content"], page["builder_json"])
    return hashlib.sha256("\x00".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()


def _manager() -> dict[str, Any]:
    user = auth.current_user()
    if not registry.is_enabled("custom_pages") or not custom_pages.can_manage(user):
        raise Refused(403, "page_builder.error.forbidden")
    return user


def _builder_page(page_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    user = _manager()
    page = custom_pages.get(page_id)
    if page is None or not custom_pages.is_builder_page(page):
        raise Refused(404, "page_builder.error.page_missing")
    return page, user


@bp.get("/admin/custom-pages/<int:page_id>/builder")
def custom_editor(page_id: int):
    user = auth.current_user()
    if not registry.is_enabled("custom_pages") or not custom_pages.can_manage(user):
        abort(403)
    page = custom_pages.get(page_id)
    if page is None or not custom_pages.is_builder_page(page):
        abort(404)
    urls = {
        **common_urls(),
        "preview": url_for("page_builder.custom_preview", page_id=page_id),
        "publish": url_for("page_builder.custom_save", page_id=page_id),
        "upload": url_for("page_builder.custom_image", page_id=page_id),
        "view": page["path"],
        "reload": url_for("page_builder.custom_editor", page_id=page_id),
        "exit": url_for("custom_pages.admin_edit", page_id=page_id),
    }
    return render_template(
        "page_builder/custom_editor.html", page=page, initial=custom_pages.builder_document(page),
        base_token=version_token(page), palette=document.PALETTE, options=editor_options(user), urls=urls,
    )


@bp.post("/api/custom-pages/<int:page_id>/builder/preview")
def custom_preview(page_id: int):
    _page, user = _builder_page(page_id)
    rate_limit("preview", user, PREVIEWS_PER_MINUTE)
    return preview_response(validated_document(json_payload(), complete=False), page_id=None)


@bp.post("/api/custom-pages/<int:page_id>/builder/save")
def custom_save(page_id: int):
    page, _user = _builder_page(page_id)
    data = json_payload()
    if data.get("base_token") != version_token(page):
        raise Refused(409, "page_builder.error.conflict")
    title = data.get("title")
    if title is not None and not isinstance(title, str):
        raise Refused(400, "page_builder.error.text_type")
    if title is not None and len(title.strip()) > custom_pages.MAX_TITLE:
        raise Refused(400, "page_builder.error.too_long")
    validated = validated_document(data, complete=True)
    custom_pages.save_builder_document(page, validated, title=title.strip() if title is not None else None)
    return jsonify({"ok": True, "redirect": page["path"]})


@bp.post("/api/custom-pages/<int:page_id>/builder/image")
def custom_image(page_id: int):
    _page, user = _builder_page(page_id)
    return store_image(user)
