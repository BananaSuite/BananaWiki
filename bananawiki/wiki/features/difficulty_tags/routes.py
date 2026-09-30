"""Setting a page's difficulty tag from the page header (1.4 URL ``/page/<slug>/tag``)."""

from __future__ import annotations

from typing import Any

from flask import abort, redirect, request

from ... import auth
from ...registry import feature_blueprint
from ..pages.blueprint import page_url, rate_limited, visible_page_or_404
from . import service
from .service import TagError

bp = feature_blueprint("difficulty_tags", "difficulty_tags", __name__, template_folder="templates",
                       static_folder="static", static_url_path="/static/difficulty_tags")


def submitted() -> tuple[str, str, str]:
    form = request.form
    return form.get("difficulty_tag", ""), form.get("tag_custom_label", ""), form.get("tag_custom_color", "")


@bp.post("/page/<slug>/tag")
@rate_limited("tag", 20)
def set_tag(slug: str):
    page = visible_page_or_404(slug)
    user = auth.current_user()
    if not service.allowed_choices(page, user):
        abort(403)
    try:
        changed = service.apply(page, user, *submitted())
    except TagError as error:
        if error.key == "difficulty_tags.error.forbidden":
            abort(403)
        auth.flash_t(error.key, "error", **error.values)
    else:
        auth.flash_t("difficulty_tags.flash.updated" if changed else "difficulty_tags.flash.unchanged",
                     "success" if changed else "info")
    return redirect(page_url(page))


def on_page_saved(page: dict[str, Any], user: dict[str, Any], **_: Any) -> None:
    """``page.saved`` interceptor: apply the tag fields of the editor form, if it had them."""
    if "difficulty_tag" not in request.form:
        return
    created = request.endpoint == "pages.create"
    try:
        service.apply(page, user, *submitted(), choices=service.permitted_choices(user) if created else None)
    except TagError as error:
        auth.flash_t(error.key, "error", **error.values)
