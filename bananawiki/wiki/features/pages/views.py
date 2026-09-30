"""Reading: home, page view, navigation, category pages and search."""

from __future__ import annotations

from typing import Any

from flask import abort, render_template, request

from ... import auth
from . import access, categories, navigation, rendering, search, service
from .blueprint import bp, rate_limited, visible_page_or_404


def _page_context(page: dict[str, Any]) -> dict[str, Any]:
    user = auth.current_user()
    body = rendering.body(page)
    previous, following = service.adjacent(page, user)
    can_edit = service.can_edit(page, user) if user else False
    return {
        "page": page,
        "body": body,
        "toc": rendering.table_of_contents(str(body)),
        "last_edit": rendering.last_edit(page),
        "breadcrumbs": _breadcrumbs(page, user),
        "previous": previous,
        "following": following,
        "can_edit": can_edit,
        "can_edit_metadata": access.can_edit_metadata(page, user) if user else False,
        "can_deindex": access.can_deindex(page, user) if user else False,
        "can_delete": service.can_delete(page, user) if user else False,
        "can_set_home": access.can_set_home(user),
        "can_view_history": bool(user) and auth.has_permission("history.view", user),
        "blocked": access.edit_blocked(page, user) if can_edit else None,
        "category_choices": access.category_choices(user) if can_edit else [],
    }


def _breadcrumbs(page: dict[str, Any], user: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [c for c in categories.ancestors(page.get("category_id")) if auth.can_read_category(c["id"], user)]


@bp.get("/")
@auth.public_read
def home():
    page = service.home()
    if page is None or not service.can_view(page):
        return render_template("pages/welcome.html", nav=navigation.tree(),
                               can_create=access.can_create_somewhere())
    return render_template("pages/page.html", **_page_context(page))


@bp.get("/page/<slug>")
@auth.public_read
def view(slug: str):
    page = visible_page_or_404(slug)
    return render_template("pages/page.html", **_page_context(page))


@bp.get("/navigation")
@auth.public_read
def navigation_page():
    user = auth.current_user()
    return render_template(
        "pages/navigation.html",
        nav=navigation.tree(user, per_category=navigation.BATCH_SIZE, total=5000),
        can_create=access.can_create_somewhere(user),
        can_create_category=bool(user) and auth.has_role("editor", user)
        and auth.has_permission("category.create", user),
    )


def _category_or_404(category_id: int) -> dict[str, Any]:
    category = categories.get(category_id)
    if category is None or not auth.can_read_category(category_id):
        abort(404)
    return category


def _batch_args() -> tuple[int | None, int]:
    try:
        category_id = int(request.args.get("category_id", "0"))
        after = int(request.args.get("after", "0"))
    except ValueError:
        abort(400)
    if not (0 <= category_id < 2**62 and 0 <= after < 2**62):
        abort(400)
    if category_id:
        _category_or_404(category_id)
    elif not auth.can_read_category(None):
        abort(404)
    return category_id or None, after


@bp.get("/category/<int:category_id>")
@auth.public_read
def category(category_id: int):
    category_row = _category_or_404(category_id)
    user = auth.current_user()
    after = request.args.get("after", 0, type=int) or 0
    try:
        listing = navigation.batch(user, category_id, after)
    except navigation.NavigationChanged:
        listing = navigation.batch(user, category_id, 0)
    return render_template(
        "pages/category.html",
        listing=listing,
        breadcrumbs=[c for c in categories.ancestors(category_id)[:-1] if auth.can_read_category(c["id"], user)],
        subcategories=[c for c in categories.all_categories()
                       if c["parent_id"] == category_id and auth.can_read_category(c["id"], user)],
        can_create_page=service.can_create(category_id, user) if user else False,
        **management_context(category_row),
    )


def management_context(category_row: dict[str, Any]) -> dict[str, Any]:
    """What the category management forms need (also served as a fragment to 1.4 scripts)."""
    user = auth.current_user()
    category_id = category_row["id"]
    choices = access.category_choices(user)
    return {
        "category": category_row,
        "page_count": categories.count_pages(category_id),
        "parent_choices": [c for c in choices
                           if c["id"] != category_id and not categories.is_descendant(c["id"], category_id)],
        "move_targets": [c for c in choices if c["id"] != category_id],
        "manage": {
            "edit": access.can_manage_category("category.edit", category_id, user),
            "move": access.can_manage_category("category.reorder", category_id, user),
            "sequential": access.can_manage_category("category.manage_sequential", category_id, user),
            "delete": access.can_manage_category("category.delete", category_id, user),
            "create": access.can_manage_category("category.create", category_id, user),
            "delete_pages": bool(user) and auth.has_permission("page.delete", user),
            "top_level": access.can_manage_category("category.reorder", None, user),
        },
    }


@bp.get("/navigation/pages")
@auth.public_read
@rate_limited("nav", 120)
def navigation_pages():
    """Paged list of one category's pages (the sidebar's fallback without JavaScript)."""
    category_id, after = _batch_args()
    try:
        listing = navigation.batch(auth.current_user(), category_id, after)
    except navigation.NavigationChanged:
        abort(409)
    return render_template("pages/navigation_pages.html", listing=listing, category_id=category_id,
                           category=categories.get(category_id) if category_id else None)


@bp.get("/search")
@auth.public_read
@rate_limited("search", 30)
def search_page():
    if not access.can_search():
        abort(403)
    raw = (request.args.get("q") or "").strip()[:300]
    scope = request.args.get("scope", "all")
    result_type = request.args.get("type", "all")
    sort = request.args.get("sort", "relevance")
    scope = scope if scope in search.SCOPES else "all"
    result_type = result_type if result_type in search.TYPES else "all"
    sort = sort if sort in search.SORTS else "relevance"
    page_number = min(max(request.args.get("page", 1, type=int) or 1, 1), 50)
    results = search.run(raw, user=auth.current_user(), scope=scope, result_type=result_type, sort=sort,
                         page=page_number)
    pages_total = max(1, -(-results["total"] // search.PER_PAGE))
    return render_template("pages/search.html", q=raw, scope=scope, result_type=result_type, sort=sort,
                           results=results, page_number=page_number, pages_total=min(pages_total, 50))
