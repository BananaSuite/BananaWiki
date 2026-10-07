"""JSON endpoints used by the editor, the sidebar and page views (1.4 URLs)."""

from __future__ import annotations

from typing import Any

from flask import abort, jsonify, render_template, request

from ... import auth, markdown, storage
from ...i18n import t
from . import access, categories, navigation, presence, rendering, search, service, uploads
from .blueprint import bp, error_text, rate_limited

MAX_REORDER = 5000


def _error(key: str, status: int, **values: Any):
    return jsonify({"error": t(key, **values)}), status


def _json_body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    return data


def _visible_page(slug: str) -> dict[str, Any]:
    page = service.get_by_slug(slug)
    if page is None or not service.can_view(page):
        abort(404)
    return page


def _category_path(paths: dict[int, str], category_id: int | None) -> str | None:
    return paths.get(category_id) if category_id else None


def _readable_paths() -> dict[int, str]:
    user = auth.current_user()
    return categories.paths(lambda cid: auth.can_read_category(cid, user))


# ── Rendering helpers for the editor ─────────────────────────────────────────


@bp.post("/api/preview")
@rate_limited("preview", 120)
def api_preview():
    content = _json_body().get("content", "")
    if not isinstance(content, str):
        abort(400)
    if len(content) > service.MAX_CONTENT:
        return _error("wiki.error.content_too_long", 413)
    return jsonify({"html": markdown.render(content, fix_lists=False)})


@bp.post("/api/code/highlight")
@rate_limited("highlight", 60)
def api_highlight():
    data = _json_body()
    content, language = data.get("content", ""), data.get("language") or ""
    if not isinstance(content, str) or not isinstance(language, str):
        abort(400)
    return jsonify({"html": markdown.highlight_code(content[: service.MAX_CONTENT], language.strip()[:64])})


# ── Lookups ───────────────────────────────────────────────────────────────────


@bp.get("/api/pages/search")
@auth.public_read
@rate_limited("lookup", 90)
def api_pages_search():
    """Title search for link pickers: ``[{title, slug, is_home, category_id, category_name}]``."""
    if not access.can_search():
        return _error("error.forbidden", 403)
    query = (request.args.get("q") or "").strip()[:200]
    if not query:
        return jsonify([])
    rows = service.search(query, limit=15, titles_only=True)
    if request.args.get("include_home") in ("1", "true", "yes"):
        home = service.home()
        if home and service.can_view(home) and query.casefold() in home["title"].casefold() \
                and all(r["id"] != home["id"] for r in rows):
            rows = [home, *rows]
    paths = _readable_paths()
    home_id = (service.home() or {}).get("id")
    return jsonify([{
        "title": row["title"], "slug": row["slug"], "is_home": row["id"] == home_id,
        "category_id": row["category_id"], "category_name": _category_path(paths, row["category_id"]),
    } for row in rows])


@bp.get("/api/sidebar/search")
@auth.public_read
@rate_limited("lookup", 90)
def api_sidebar_search():
    """Live sidebar search: ``{categories: [...], pages: [...]}``."""
    if not access.can_search():
        return _error("error.forbidden", 403)
    query = (request.args.get("q") or "").strip()[:200]
    if not query:
        return jsonify({"categories": [], "pages": []})
    titles_only = request.args.get("scope", "title") != "content"
    pages = service.search(query, limit=20, titles_only=titles_only)
    paths = _readable_paths()
    user = auth.current_user()
    found = [c for c in categories.search(query, limit=10) if categories.listed(c["id"], user)]
    return jsonify({
        "categories": [{"id": c["id"], "name": c["name"], "path": paths.get(c["id"], c["name"]),
                        "parent_id": c["parent_id"]} for c in found],
        "pages": [{
            "id": p["id"], "title": p["title"], "slug": p["slug"], "category_id": p["category_id"],
            "category_name": _category_path(paths, p["category_id"]),
            "snippet": "" if titles_only else str(search.snippet_html(p.get("snippet") or "", [query],
                                                                        marked=service.fts_available())),
        } for p in pages],
    })


@bp.get("/api/pages/preview-by-slug")
@auth.public_read
@rate_limited("lookup", 90)
def api_preview_by_slug():
    slug = (request.args.get("slug") or "").strip()
    if not slug:
        return _error("pages.error.slug_required", 400)
    page = _visible_page(slug)
    return jsonify({"title": page["title"], "slug": page["slug"], "is_home": bool(page["is_home"]),
                    "html": str(rendering.body(page))})


@bp.get("/api/sidebar/pages")
@auth.public_read
@rate_limited("nav", 120)
def api_sidebar_pages():
    """The next batch of a category's links in the sidebar: ``{html, more}``."""
    from .views import _batch_args

    category_id, after = _batch_args()
    try:
        listing = navigation.batch(auth.current_user(), category_id, after)
    except navigation.NavigationChanged:
        return _error("pages.error.navigation_changed", 409)
    html = render_template("pages/_nav_pages.html", pages=listing["pages"], more=listing["more"],
                           category_id=category_id, current_slug=None, reorder=may_reorder_pages())
    return jsonify({"html": html, "more": listing["more"]})


def may_reorder_pages() -> bool:
    user = auth.current_user()
    return bool(user) and (auth.is_admin(user) or (auth.has_role("editor", user)
                                                   and auth.has_permission("page.edit_all", user)))


# ── Live page state ───────────────────────────────────────────────────────────


@bp.get("/api/page/<slug>/sync")
@rate_limited("sync", 120)
def api_page_sync(slug: str):
    """Has the page changed since the reader loaded it? ``?since=<revision>``."""
    page = _visible_page(slug)
    since = request.args.get("since", "")
    if not since.isdigit():
        return jsonify({"changed": True, "reload": True, "revision": page["revision"]})
    changed = int(page["revision"] or 0) > int(since)
    payload: dict[str, Any] = {"changed": changed, "revision": page["revision"]}
    if changed:
        editor = rendering.last_edit(page) or {}
        payload.update({"title": page["title"], "edited_by": editor.get("username"),
                        "last_edited_at": page["last_edited_at"]})
    return jsonify(payload)


def _presence_page(slug: str) -> dict[str, Any]:
    page = _visible_page(slug)
    if not service.can_edit(page):
        abort(403)
    return page


@bp.post("/api/page/<slug>/editing/heartbeat")
@rate_limited("presence", 60)
def api_editing_heartbeat(slug: str):
    page = _presence_page(slug)
    user = auth.current_user()
    presence.heartbeat(page["id"], user)
    return jsonify({"ok": True, "editors": presence.active_editors(page["id"], exclude_user_id=user["id"]),
                    "revision": page["revision"]})


@bp.get("/api/page/<slug>/editing/check")
@rate_limited("presence", 60)
def api_editing_check(slug: str):
    page = _presence_page(slug)
    return jsonify({"editors": presence.active_editors(page["id"], exclude_user_id=auth.current_user()["id"])})


@bp.post("/api/page/<slug>/editing/stop")
@rate_limited("presence", 60)
def api_editing_stop(slug: str):
    page = service.get_by_slug(slug)
    if page is not None:
        presence.stop(page["id"], auth.current_user()["id"])
    return jsonify({"ok": True})


# ── Images ────────────────────────────────────────────────────────────────────


@bp.post("/api/upload")
@rate_limited("upload", 20)
def api_upload():
    user = auth.current_user()
    if not uploads.may_upload(user):
        return _error("error.forbidden", 403)
    try:
        stored = uploads.save_image(request.files.get("file"), user)
    except storage.UploadError as exc:
        status = 429 if isinstance(exc, uploads.QuotaExceeded) else 400
        return jsonify({"error": error_text(exc)}), status
    return jsonify(stored), 201


@bp.post("/api/upload/delete")
@rate_limited("upload", 20)
def api_upload_delete():
    """Administrators only; images still used somewhere need ``force``."""
    if not auth.is_admin():
        return _error("error.forbidden", 403)
    data = _json_body()
    filename = str(data.get("filename") or "").rsplit("/", 1)[-1]
    if not filename or storage.resolve("uploads", filename) is None:
        return _error("pages.error.upload_missing", 404)
    if not data.get("force") and uploads.is_referenced(filename):
        return _error("pages.error.upload_in_use", 409)
    storage.delete("uploads", filename)
    return jsonify({"ok": True})


# ── Ordering and home page ───────────────────────────────────────────────────


def _ids() -> list[int]:
    raw = _json_body().get("ids")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_REORDER:
        abort(400)
    try:
        ids = [int(item) for item in raw]
    except (TypeError, ValueError):
        abort(400)
    if len(set(ids)) != len(ids) or any(not 0 < item < 2**62 for item in ids):
        abort(400)
    return ids


@bp.post("/api/reorder/pages")
@rate_limited("reorder", 60)
def api_reorder_pages():
    """Body ``{"ids": [...]}``: the new order of pages (grouped by their category)."""
    ids = _ids()
    groups: dict[int | None, list[int]] = {}
    for page_id in ids:
        page = service.get(page_id, with_content=False)
        if page is None or not service.can_view(page):
            return _error("pages.error.page_missing", 404)
        if page["is_home"]:
            continue
        if not service.can_edit(page):
            return _error("error.forbidden", 403)
        groups.setdefault(page["category_id"], []).append(page_id)
    for category_id, members in groups.items():
        categories.reorder_pages(members, category_id)
    return jsonify({"ok": True, "message": t("pages.flash.order_saved")})


@bp.post("/api/reorder/categories")
@rate_limited("reorder", 60)
def api_reorder_categories():
    """Body ``{"ids": [...]}``: the new order of sibling categories."""
    ids = _ids()
    parents = set()
    for category_id in ids:
        category = categories.get(category_id)
        if category is None or not auth.can_read_category(category_id):
            return _error("pages.error.category_missing", 404)
        if not access.can_manage_category("category.reorder", category_id):
            return _error("error.forbidden", 403)
        parents.add(category["parent_id"])
    if len(parents) != 1:
        return _error("pages.error.not_siblings", 400)
    categories.reorder_categories(ids, parents.pop())
    return jsonify({"ok": True, "message": t("pages.flash.order_saved")})


@bp.post("/api/pages/<int:page_id>/home")
@rate_limited("details", 10)
def api_set_home(page_id: int):
    if not access.can_set_home():
        return _error("error.forbidden", 403)
    page = service.get(page_id)
    if page is None:
        return _error("pages.error.page_missing", 404)
    already = bool(page["is_home"])
    service.set_home(page)
    home = service.get(page_id)
    return jsonify({"ok": True, "already_home": already,
                    "message": t("pages.flash.home_set", title=home["title"]),
                    "home_page": {"id": home["id"], "title": home["title"], "slug": home["slug"],
                                  "is_home": True, "is_deindexed": bool(home["is_deindexed"]),
                                  "pending_deletion": bool(home["pending_deletion"])}})


@bp.get("/api/category/<int:category_id>/management")
@rate_limited("lookup", 60)
def api_category_management(category_id: int):
    """1.4 endpoint: the management form of a category as an HTML fragment."""
    category = categories.get(category_id)
    if category is None or not auth.can_read_category(category_id) or not auth.has_role("editor"):
        abort(404)
    from .views import management_context

    return jsonify({"html": render_template("pages/_category_manage.html", **management_context(category))})
