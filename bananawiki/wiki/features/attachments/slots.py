"""The attachment lists on the page view and in the editor."""

from __future__ import annotations

from typing import Any

from flask import render_template

from ... import auth
from . import service


def _context(page: dict[str, Any]) -> dict[str, Any] | None:
    user = auth.current_user()
    if user is None:
        return None
    can_view = service.can_download(page, user)
    can_upload = service.can_upload(page, user)
    attachments = service.for_page(page["id"]) if (can_view or can_upload) else []
    if not attachments and not can_upload:
        return None
    return {
        "page": page,
        "attachments": attachments,
        "can_view": can_view,
        "can_upload": can_upload,
        "deletable": {a["id"] for a in attachments if service.can_delete(a, page, user)},
        "max_bytes": service.max_bytes(),
    }


def page_panel(page: dict[str, Any]) -> str:
    """Attachment list with download, upload and delete forms (``page.below_content``)."""
    context = _context(page)
    return render_template("attachments/_page_panel.html", **context) if context else ""


def editor_panel(page: dict[str, Any] | None) -> str:
    """Attachment list with script-driven upload and delete (``editor.below_form``).

    New pages have no attachments yet; files are added after the first save.
    """
    if page is None:
        return ""
    context = _context(page)
    return render_template("attachments/_editor_panel.html", **context) if context else ""
