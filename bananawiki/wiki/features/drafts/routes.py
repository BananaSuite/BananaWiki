"""Draft routes: the 1.4 JSON API used by the editor script, and the "my drafts" page."""

from __future__ import annotations

from typing import Any

from flask import abort, jsonify, redirect, render_template, request, url_for

from ....core.web import safe_next
from ... import auth
from ...i18n import t
from ...registry import feature_blueprint
from ..pages import service as pages
from ..pages.blueprint import rate_limited
from . import service
from .service import DraftError

bp = feature_blueprint("drafts", "drafts", __name__, template_folder="templates",
                       static_folder="static", static_url_path="/static/drafts")

# A 1 MB page as JSON (escaped, UTF-8) plus the title stays well below this.
MAX_BODY = 8 * 1024 * 1024


def _error(key: str, status: int, **values: Any):
    return jsonify({"error": t(key, **values)}), status


def _body() -> dict[str, Any] | None:
    if request.content_length is None or request.content_length > MAX_BODY:
        return None
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _page_id(value: Any) -> int | None:
    try:
        page_id = int(value)
    except (TypeError, ValueError):
        return None
    return page_id if 0 < page_id < 2**62 else None


def _visible_page(page_id: int | None) -> dict[str, Any] | None:
    page = pages.get(page_id) if page_id else None
    return page if page is not None and pages.can_view(page) else None


# ── JSON API (1.4 URLs) ──────────────────────────────────────────────────────


@bp.post("/api/draft/save")
@rate_limited("draft-save", 40)
def api_save():
    if request.content_length is not None and request.content_length > MAX_BODY:
        return _error("drafts.error.too_large", 413)
    data = _body()
    if data is None:
        return _error("drafts.error.invalid", 400)
    page = _visible_page(_page_id(data.get("page_id")))
    if page is None:
        return _error("drafts.error.page_missing", 404)
    user = auth.current_user()
    if not service.can_save(page, user):
        return _error("error.forbidden", 403)
    revision = data.get("revision")
    base = revision if isinstance(revision, int) and not isinstance(revision, bool) else None
    try:
        status = service.save(page, user, data.get("title", page["title"]), data.get("content"), base)
    except DraftError as error:
        return _error(error.key, 400, **error.values)
    return jsonify({"ok": True, "status": status})


@bp.get("/api/draft/load/<int:page_id>")
@rate_limited("draft-read", 60)
def api_load(page_id: int):
    page = _visible_page(page_id)
    if page is None:
        return _error("drafts.error.page_missing", 404)
    user = auth.current_user()
    if not (service.can_edit(page, user) and auth.has_permission("draft.view_own", user)):
        return _error("error.forbidden", 403)
    draft = service.get(page_id, user["id"])
    if draft is None:
        return jsonify({"title": None, "content": None, "updated_at": None})
    return jsonify({"title": draft["title"], "content": draft["content"], "updated_at": draft["updated_at"]})


@bp.get("/api/draft/others/<int:page_id>")
@rate_limited("draft-read", 60)
def api_others(page_id: int):
    page = _visible_page(page_id)
    if page is None:
        return _error("drafts.error.page_missing", 404)
    user = auth.current_user()
    if not service.can_edit(page, user):
        return _error("error.forbidden", 403)
    drafts = [{"username": row["username"], "user_id": row["user_id"], "updated_at": row["updated_at"]}
              for row in service.others(page_id, user["id"])]
    return jsonify({"drafts": drafts, "page_last_edited_at": page["last_edited_at"], "revision": page["revision"],
                    "can_transfer": service.can_transfer(page, user)})


@bp.post("/api/draft/transfer")
@rate_limited("draft-change", 30)
def api_transfer():
    data = _body()
    if data is None or not isinstance(data.get("from_user_id"), str):
        return _error("drafts.error.invalid", 400)
    page = _visible_page(_page_id(data.get("page_id")))
    if page is None:
        return _error("drafts.error.page_missing", 404)
    user = auth.current_user()
    if not service.can_transfer(page, user):
        return _error("error.forbidden", 403)
    try:
        service.transfer(page["id"], data["from_user_id"], user["id"])
    except DraftError as error:
        return _error(error.key, 404 if error.key == "drafts.error.missing" else 400)
    return jsonify({"ok": True})


@bp.post("/api/draft/delete")
@rate_limited("draft-change", 30)
def api_delete():
    data = _body()
    page_id = _page_id(data.get("page_id")) if data else None
    if page_id is None:
        return _error("drafts.error.invalid", 400)
    if not auth.has_permission("draft.delete_own"):
        return _error("error.forbidden", 403)
    service.delete(page_id, auth.current_user()["id"])
    return jsonify({"ok": True})


@bp.get("/api/draft/mine")
@rate_limited("draft-read", 60)
def api_mine():
    if not auth.has_permission("draft.view_own"):
        return _error("error.forbidden", 403)
    return jsonify([
        {"page_id": d["page_id"], "page_title": d["page_title"], "page_slug": d["page_slug"], "title": d["title"],
         "updated_at": d["updated_at"]}
        for d in _my_drafts() if d["page_slug"]
    ])


# ── "My drafts" page and form fallbacks ──────────────────────────────────────


def _my_drafts() -> list[dict[str, Any]]:
    """The user's drafts; pages they can no longer see keep only the draft's own title."""
    user = auth.current_user()
    result = []
    for draft in service.of_user(user["id"]):
        page = pages.get(draft["page_id"], with_content=False)
        visible = page is not None and pages.can_view(page, user)
        result.append({
            **draft,
            "page_title": page["title"] if visible else None,
            "page_slug": page["slug"] if visible else None,
            "editable": visible and service.can_edit(page, user),
        })
    return result


@bp.get("/drafts")
@auth.permission_required("draft.view_own")
def mine():
    return render_template("drafts/mine.html", drafts=_my_drafts(),
                           can_delete=auth.has_permission("draft.delete_own"))


def _back(page_id: int):
    page = pages.get(page_id, with_content=False)
    fallback = url_for("drafts.mine")
    if page is not None and pages.can_view(page):
        fallback = url_for("pages.edit", slug=page["slug"])
    return redirect(safe_next(fallback, request.form.get("next")))


@bp.post("/drafts/<int:page_id>/discard")
@auth.permission_required("draft.delete_own")
@rate_limited("draft-change", 30)
def discard(page_id: int):
    if service.delete(page_id, auth.current_user()["id"]):
        auth.flash_t("drafts.flash.discarded", "success")
    return _back(page_id)


@bp.post("/drafts/<int:page_id>/transfer")
@rate_limited("draft-change", 30)
def transfer(page_id: int):
    page = _visible_page(page_id)
    if page is None:
        abort(404)
    if not service.can_transfer(page):
        abort(403)
    try:
        service.transfer(page_id, request.form.get("from_user_id", ""), auth.current_user()["id"])
    except DraftError as error:
        auth.flash_t(error.key, "error")
    else:
        auth.flash_t("drafts.flash.transferred", "success")
    return redirect(url_for("pages.edit", slug=page["slug"]))
