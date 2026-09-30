"""Export links in the page header and "load a Markdown file" in the editor."""

from __future__ import annotations

from typing import Any

from flask import render_template

from ... import auth
from ..pages import service as pages
from .routes import markdown_allowed, pdf_allowed


def header_actions(page: dict[str, Any]) -> str:
    user = auth.current_user()
    if user is None:
        return ""
    can_pdf, can_md = pdf_allowed(user), markdown_allowed(page, user)
    if not (can_pdf or can_md):
        return ""
    return render_template("page_export/_header_actions.html", page=page, can_pdf=can_pdf, can_md=can_md)


def editor_import(page: dict[str, Any] | None) -> str:
    if page is None or not pages.can_edit(page):
        return ""
    return render_template("page_export/_editor_import.html", page=page)
