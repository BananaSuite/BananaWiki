"""Categories: list, read, create, rename, move, sequential navigation and delete.

Permissions are the category manager's: ``category.create``, ``category.edit``
(rename), ``category.reorder`` (move), ``category.manage_sequential`` and
``category.delete``, each with write access to the category concerned (the
new parent too when moving). A category the caller cannot read answers 404;
the list also needs ``category.view_all``.
"""

from __future__ import annotations

from typing import Any

from flask import request

from .... import auth
from ...pages import access, categories, service
from .. import serialize, tokens
from ..errors import (
    ApiError,
    flag,
    from_service,
    invalid,
    json_body,
    optional_id,
    page_window,
    text,
    window_fields,
)
from . import bp, caller, caller_token, ok, requires


def _readable(category_id: int) -> dict[str, Any]:
    category = categories.get(category_id)
    if category is None or not auth.can_read_category(category["id"], caller()):
        raise ApiError(404, "category_not_found")
    return category


def _allowed(permission: str, category_id: int | None) -> None:
    if not access.can_manage_category(permission, category_id, caller()):
        raise ApiError(403, "cannot_manage_category")


def _parent(value: Any) -> int | None:
    parent_id = optional_id(value, "parent_id")
    if parent_id is not None and (categories.get(parent_id) is None
                                  or not auth.can_read_category(parent_id, caller())):
        raise ApiError(400, "parent_missing")
    return parent_id


@bp.get("/categories")
@requires("categories")
def list_categories():
    user = caller()
    limit, offset = page_window()
    rows = [row for row in categories.all_categories() if categories.listed(row["id"], user)]
    window = rows[offset:offset + limit + 1]
    return ok(categories=[serialize.category(row) for row in window[:limit]],
              **window_fields(limit, offset, len(window)))


@bp.get("/categories/<int:category_id>")
@requires("categories")
def get_category(category_id: int):
    category = _readable(category_id)
    pages = service.list_visible(category_id=category["id"], user=caller(), order="sort")
    return ok(category={**serialize.category(category),
                        "pages": [serialize.page_summary(page) for page in pages]})


@bp.post("/categories")
@requires("categories", write=True)
def create_category():
    data = json_body()
    name = text(data.get("name"), "name", maximum=categories.MAX_NAME, required=True)
    parent_id = _parent(data.get("parent_id"))
    _allowed("category.create", parent_id)
    try:
        category = categories.create(name, parent_id, actor_id=caller()["id"])
    except categories.CategoryError as error:
        raise from_service(error) from None
    return ok(201, category=serialize.category(category))


@bp.put("/categories/<int:category_id>")
@requires("categories", write=True)
def update_category(category_id: int):
    """Everything is checked before anything changes, so a refused request leaves the category as it was."""
    category = _readable(category_id)
    data = json_body()
    name = sequential = None
    moving = "parent_id" in data
    parent_id = None
    if "name" in data:
        name = text(data["name"], "name", maximum=categories.MAX_NAME, required=True)
        _allowed("category.edit", category["id"])
    if moving:
        parent_id = _parent(data["parent_id"])
        if parent_id is not None and (parent_id == category["id"]
                                      or categories.is_descendant(parent_id, category["id"])):
            raise ApiError(400, "category_cycle")
        _allowed("category.reorder", category["id"])
        _allowed("category.reorder", parent_id)
    if "sequential_nav" in data:
        sequential = flag(data["sequential_nav"], "sequential_nav")
        _allowed("category.manage_sequential", category["id"])
    try:
        if name is not None:
            category = categories.rename(category, name)
        if moving and parent_id != category.get("parent_id"):
            category = categories.set_parent(category, parent_id)
        if sequential is not None:
            categories.set_sequential(category, sequential)
    except categories.CategoryError as error:
        raise from_service(error) from None
    return ok(category=serialize.category(categories.get(category["id"]) or category))


@bp.delete("/categories/<int:category_id>")
@requires("categories", write=True)
def delete_category(category_id: int):
    """Pages are uncategorised by default; ``?page_action=move&target_id=…`` or ``delete`` also work.

    ``delete`` also needs the token's ``pages`` write scope and follows the web
    interface (:func:`categories.delete_with_pages`): pages the caller cannot
    see or delete answer 403, protected, checked-out or pending pages 409, and
    nothing changes; pages that Deletion Slowdown schedules instead make the
    answer 202.
    """
    category = _readable(category_id)
    _allowed("category.delete", category["id"])
    action = request.args.get("page_action", "uncategorize")
    if action not in ("uncategorize", "move", "delete"):
        raise invalid("page_action", "choice", options="uncategorize, move, delete")
    target_id = optional_id(request.args.get("target_id"), "target_id")
    if action == "move":
        if target_id is None or categories.get(target_id) is None:
            raise ApiError(400, "category_missing")
        if not auth.can_write_category(target_id, caller()):
            raise ApiError(403, "cannot_manage_category")
    if action == "delete":
        # Deleting pages is a pages operation: the token must allow it as for DELETE /pages/<slug>.
        if not tokens.parse_grant(caller_token()["permissions"]).allows("pages", write=True):
            raise ApiError(403, "scope_missing", extra={"scope": "pages", "write": True}, scope="pages")
        if not auth.has_permission("page.delete", caller()):
            raise ApiError(403, "cannot_delete")
    try:
        removal = categories.delete_with_pages(category, caller(), page_action=action, target_id=target_id)
    except categories.PagesRefused as error:
        raise ApiError(409 if error.reason == "blocked" else 403, error.key.rsplit(".", 1)[-1], error.key,
                       extra={"pages": [page["slug"] for page in error.pages]}, **error.values) from None
    except categories.CategoryError as error:
        raise from_service(error) from None
    if action != "delete":
        return ok(deleted=True, id=category_id)
    return ok(202 if removal.pending else 200, deleted=True, id=category_id,
              pending_deletion=list(removal.pending), kept=list(removal.kept))
