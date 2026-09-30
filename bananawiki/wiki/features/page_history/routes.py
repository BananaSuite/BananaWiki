"""Page history: list, single versions with diffs, revert, deletion and attribution.

The routes are attached to the ``pages`` blueprint (the canonical endpoint
``pages.history`` belongs to it) and answer 404 while the feature is off.
Permissions: ``history.view`` to read, ``history.revert``, ``history.delete``
and ``history.transfer`` (each also needs edit access to the page).
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from flask import Blueprint, abort, redirect, render_template, request, url_for

from ... import auth, registry
from ...db import db
from ..pages import access, diff, rendering, service
from ..pages.blueprint import page_url, rate_limited, visible_page_or_404

PER_PAGE = 50
VIEWS = ("rendered", "diff", "source", "source_diff")


def _enabled(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if not registry.is_enabled("page_history"):
            abort(404)
        return view(*args, **kwargs)

    return wrapper


def _readable_page(slug: str) -> dict[str, Any]:
    page = visible_page_or_404(slug)
    if not auth.has_permission("history.view"):
        abort(403)
    return page


def _managed_page(slug: str, permission: str) -> dict[str, Any]:
    page = _readable_page(slug)
    if not auth.has_permission(permission) or not service.can_edit(page):
        abort(403)
    return page


def _entry_or_404(page: dict[str, Any], entry_id: int) -> dict[str, Any]:
    entry = service.history_entry(entry_id)
    if entry is None or entry["page_id"] != page["id"]:
        abort(404)
    return entry


def _history_rows(page_id: int, offset: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT * FROM (SELECT h.id, h.title, h.edited_by, h.edit_message, h.is_revert, h.created_at, "
        "length(h.content) AS size, length(h.content) - LAG(length(h.content)) OVER (ORDER BY h.id) AS delta, "
        "u.username AS editor FROM page_history h LEFT JOIN users u ON u.id = h.edited_by WHERE h.page_id = ?) "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_id, PER_PAGE, offset),
    )


def attach(bp: Blueprint) -> None:
    """Register the history routes on the pages blueprint."""

    @bp.get("/page/<slug>/history", endpoint="history")
    @_enabled
    def history_list(slug: str):
        page = _readable_page(slug)
        number = max(request.args.get("page", 1, type=int) or 1, 1)
        total = int(db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page["id"],), default=0))
        rows = _history_rows(page["id"], (number - 1) * PER_PAGE)
        for row in rows:
            row["profile_url"] = rendering.profile_url(row["editor"])
        can_manage = service.can_edit(page)
        return render_template(
            "page_history/list.html", page=page, rows=rows, number=number,
            pages_total=max(1, -(-total // PER_PAGE)), total=total,
            can_revert=can_manage and auth.has_permission("history.revert"),
            can_delete=can_manage and auth.has_permission("history.delete"),
            can_transfer=can_manage and auth.has_permission("history.transfer"),
            blocked=access.edit_blocked(page) if can_manage else None,
        )

    @bp.get("/page/<slug>/history/<int:entry_id>", endpoint="history_entry")
    @_enabled
    def history_entry(slug: str, entry_id: int):
        page = _readable_page(slug)
        entry = _entry_or_404(page, entry_id)
        against_id = request.args.get("against", type=int)
        if against_id:
            previous = _entry_or_404(page, against_id)
        else:
            previous = db.one("SELECT * FROM page_history WHERE page_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
                              (page["id"], entry_id))
        view = request.args.get("view", "rendered")
        view = view if view in VIEWS else "rendered"
        if previous is None and view in ("diff", "source_diff"):
            view = "rendered"
        version = {**page, "title": entry["title"], "content": entry["content"],
                   "builder_json": entry.get("builder_json") or "", "builder_public": entry.get("builder_public")}
        body = rendering.body(version)
        shown: Any = body
        if view == "diff":
            old = {**version, "content": previous["content"], "builder_json": previous.get("builder_json") or ""}
            shown = diff.rendered_diff(str(rendering.body(old)), str(body))
        elif view == "source_diff":
            shown = diff.source_diff(previous["content"], entry["content"])
        can_manage = service.can_edit(page)
        return render_template(
            "page_history/entry.html", page=page, entry=entry, previous=previous, view=view, shown=shown,
            editor_url=rendering.profile_url(entry["editor"]),
            can_revert=can_manage and auth.has_permission("history.revert"),
        )

    @bp.post("/page/<slug>/revert/<int:entry_id>", endpoint="history_revert")
    @_enabled
    @rate_limited("history", 20)
    def revert(slug: str, entry_id: int):
        page = _managed_page(slug, "history.revert")
        _entry_or_404(page, entry_id)
        blocked = access.edit_blocked(page)
        if blocked:
            auth.flash_t(blocked, "error")
            return redirect(page_url(page))
        try:
            service.revert(page, entry_id, author_id=auth.current_user()["id"])
        except service.PageError as exc:
            auth.flash_t(exc.key, "error", **exc.values)
            return redirect(url_for("pages.history", slug=slug))
        auth.flash_t("page_history.flash.reverted", "success")
        return redirect(page_url(page))

    @bp.post("/page/<slug>/history/<int:entry_id>/delete", endpoint="history_delete_entry")
    @_enabled
    @rate_limited("history", 30)
    def delete_entry(slug: str, entry_id: int):
        page = _managed_page(slug, "history.delete")
        _entry_or_404(page, entry_id)
        service.delete_history_entry(page["id"], entry_id)
        auth.flash_t("page_history.flash.entry_deleted", "success")
        return redirect(url_for("pages.history", slug=slug))

    @bp.post("/page/<slug>/history/clear", endpoint="history_clear")
    @_enabled
    @rate_limited("history", 10)
    def clear(slug: str):
        page = _managed_page(slug, "history.delete")
        count = service.clear_history(page["id"])
        auth.flash_t("page_history.flash.cleared", "success", count=count)
        return redirect(url_for("pages.history", slug=slug))

    @bp.post("/page/<slug>/history/<int:entry_id>/transfer", endpoint="history_transfer")
    @_enabled
    @rate_limited("history", 30)
    def transfer(slug: str, entry_id: int):
        page = _managed_page(slug, "history.transfer")
        _entry_or_404(page, entry_id)
        target = _target_user("new_user_id", "username")
        if target is None:
            auth.flash_t("page_history.error.user_missing", "error")
        else:
            service.transfer_history(page["id"], entry_id, target["id"])
            auth.flash_t("page_history.flash.transferred", "success", username=target["username"])
        return redirect(url_for("pages.history", slug=slug))

    @bp.post("/page/<slug>/history/bulk-transfer", endpoint="history_bulk_transfer")
    @_enabled
    @rate_limited("history", 20)
    def bulk_transfer(slug: str):
        page = _managed_page(slug, "history.transfer")
        source = _target_user("from_user_id", "from_username")
        target = _target_user("new_user_id", "username")
        if source is None or target is None:
            auth.flash_t("page_history.error.user_missing", "error")
        else:
            count = service.transfer_history_bulk(page["id"], source["id"], target["id"])
            auth.flash_t("page_history.flash.bulk_transferred", "success", count=count,
                         username=target["username"])
        return redirect(url_for("pages.history", slug=slug))

    @bp.post("/page/<slug>/history/<int:entry_id>/deattribute", endpoint="history_deattribute")
    @_enabled
    @rate_limited("history", 30)
    def deattribute(slug: str, entry_id: int):
        page = _managed_page(slug, "history.transfer")
        _entry_or_404(page, entry_id)
        service.transfer_history(page["id"], entry_id, None)
        auth.flash_t("page_history.flash.deattributed", "success")
        return redirect(url_for("pages.history", slug=slug))


def _target_user(id_field: str, name_field: str) -> dict[str, Any] | None:
    user_id = (request.form.get(id_field) or "").strip()
    username = (request.form.get(name_field) or "").strip()
    if user_id:
        return db.one("SELECT id, username FROM users WHERE id = ?", (user_id,))
    if username:
        return db.one("SELECT id, username FROM users WHERE username = ? COLLATE NOCASE", (username,))
    return None
