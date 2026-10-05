"""Category management: create, rename, move, sequential navigation, delete.

Every action needs its ``category.*`` permission *and* write access to the
categories it touches (fixes 1.4, where restricted editors could rename or
move any category).
"""

from __future__ import annotations

from typing import Any

from flask import abort, redirect, request, url_for

from ... import auth
from . import access, categories
from .blueprint import back, bp, rate_limited


def _category_or_404(category_id: int) -> dict[str, Any]:
    category = categories.get(category_id)
    if category is None or not auth.can_read_category(category_id):
        abort(404)
    return category


def _optional_id(name: str) -> int | None:
    value = request.form.get(name)
    if value in (None, "", "0"):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        abort(400)


def _require(permission: str, category_id: int | None) -> None:
    if not access.can_manage_category(permission, category_id):
        abort(403)


@bp.post("/category/create")
@rate_limited("category", 20)
def create_category():
    parent_id = _optional_id("parent_id")
    if parent_id is not None:
        _category_or_404(parent_id)
    _require("category.create", parent_id)
    try:
        category = categories.create(request.form.get("name", ""), parent_id, actor_id=auth.current_user()["id"])
    except categories.CategoryError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
        return back()
    auth.flash_t("pages.flash.category_created", "success", name=category["name"])
    return back(url_for("pages.category", category_id=category["id"]))


@bp.post("/category/<int:category_id>/edit")
@rate_limited("category", 20)
def rename_category(category_id: int):
    category = _category_or_404(category_id)
    _require("category.edit", category_id)
    try:
        categories.rename(category, request.form.get("name", ""))
    except categories.CategoryError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
    else:
        auth.flash_t("pages.flash.category_renamed", "success")
    return back(url_for("pages.category", category_id=category_id))


@bp.post("/category/<int:category_id>/move")
@rate_limited("category", 20)
def move_category(category_id: int):
    category = _category_or_404(category_id)
    parent_id = _optional_id("parent_id")
    if parent_id is not None:
        _category_or_404(parent_id)
    _require("category.reorder", category_id)
    _require("category.reorder", parent_id)
    try:
        categories.set_parent(category, parent_id)
    except categories.CategoryError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
    else:
        auth.flash_t("pages.flash.category_moved", "success")
    return back(url_for("pages.category", category_id=category_id))


@bp.post("/category/<int:category_id>/sequential-nav")
@rate_limited("category", 20)
def sequential_nav(category_id: int):
    category = _category_or_404(category_id)
    _require("category.manage_sequential", category_id)
    categories.set_sequential(category, request.form.get("sequential_nav") == "1"
                              or "1" in request.form.getlist("sequential_nav"))
    auth.flash_t("pages.flash.sequential_saved", "success")
    return back(url_for("pages.category", category_id=category_id))


@bp.post("/category/<int:category_id>/delete")
@rate_limited("category", 10)
def delete_category(category_id: int):
    """Delete a category; its pages are uncategorised, moved or deleted.

    Deleting the pages follows the rules of deleting each page on its own;
    if any page may not go, nothing changes (:func:`categories.delete_with_pages`,
    shared with the API and bulk deletion).
    """
    category = _category_or_404(category_id)
    _require("category.delete", category_id)
    user = auth.current_user()
    action = request.form.get("page_action", "uncategorize")
    if action not in ("uncategorize", "move", "delete"):
        abort(400)
    target_id = _optional_id("target_category_id") if action == "move" else None
    if action == "move":
        if target_id is None or target_id == category_id:
            auth.flash_t("pages.error.choose_target", "error")
            return back()
        _category_or_404(target_id)
        if not auth.can_write_category(target_id, user):
            abort(403)
    try:
        categories.delete_with_pages(category, user, page_action=action, target_id=target_id)
    except categories.CategoryError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
        return back()
    auth.flash_t("pages.flash.category_deleted", "success", name=category["name"])
    return redirect(url_for("pages.navigation_page"))
