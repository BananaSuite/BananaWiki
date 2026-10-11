"""Routes: /admin/bulk and its delete actions (1.4 URLs).

Every deletion goes through the owning feature's service, so history, files,
events and interceptors (page protection, deletion slowdown) apply exactly as
for a single deletion. Canvases and boards have no grace period, and pages
only while deletion slowdown is on, so every bulk deletion needs a typed
confirmation word checked here (not only a browser dialog) and is written to
the audit log with what it removed.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, redirect, render_template, request, url_for

from ... import auth, registry
from ...db import db
from ...i18n import t
from ..audit.service import record
from ..pages import categories
from ..pages import service as pages

bp = Blueprint("bulk_manage", __name__, template_folder="templates")

MAX_IDS = 500
CONFIRM_WORD = "DELETE"  # always accepted, besides the word in the interface language
MAX_AUDITED_NAMES = 50


def _ids(field: str = "ids") -> list[int]:
    values = []
    for raw in request.form.getlist(field)[:MAX_IDS]:
        try:
            values.append(int(raw))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(values))


def _done(key: str, **counts: Any):
    auth.flash_t(key, "success", **counts)
    return redirect(url_for("bulk_manage.index"))


def _confirmed() -> bool:
    """Whether the form carries the typed confirmation word; flashes why not."""
    typed = " ".join((request.form.get("confirm_text") or "").split()).casefold()
    if typed and typed in (CONFIRM_WORD.casefold(), t("bulk_manage.confirm_word").casefold()):
        return True
    auth.flash_t("bulk_manage.error.confirm", "error", word=t("bulk_manage.confirm_word"))
    return False


def _refused():
    return redirect(url_for("bulk_manage.index"))


def _audit(kind: str, names: list[str], **counts: Any) -> None:
    if names or any(counts.values()):
        record("bulk.deleted", details={"kind": kind, "count": len(names), **counts,
                                        "items": ", ".join(names[:MAX_AUDITED_NAMES])
                                        + (" …" if len(names) > MAX_AUDITED_NAMES else "")})


@bp.get("/admin/bulk")
@auth.admin_required
def index():
    data: dict[str, Any] = {
        "categories": db.all(
            "SELECT c.id, c.name, (SELECT COUNT(*) FROM pages p WHERE p.category_id = c.id) AS pages "
            "FROM categories c ORDER BY c.name COLLATE NOCASE"),
        "pages": db.all("SELECT id, title, slug, is_home, pending_deletion FROM pages ORDER BY title COLLATE NOCASE"),
        "canvases": [], "boards": [],
    }
    if registry.is_enabled("canvas"):
        data["canvases"] = db.all("SELECT id, title, slug FROM canvas__layouts ORDER BY title COLLATE NOCASE")
    if registry.is_enabled("kanban"):
        data["boards"] = db.all("SELECT id, title FROM kanban_boards ORDER BY title COLLATE NOCASE")
    return render_template("bulk_manage/index.html", **data)


@bp.post("/admin/bulk/categories/delete")
@auth.admin_required
def delete_categories():
    """A category whose pages cannot all be deleted is skipped and keeps them."""
    if not _confirmed():
        return _refused()
    user = auth.current_user()
    action = request.form.get("page_action", "uncategorize")
    if action not in ("uncategorize", "delete"):
        action = "uncategorize"
    deleted = skipped = 0
    names: list[str] = []
    for category_id in _ids():
        category = categories.get(category_id)
        if category is None:
            continue
        try:
            categories.delete_with_pages(category, user, page_action=action)
        except categories.PagesRefused:
            skipped += 1
            continue
        deleted += 1
        names.append(category["name"])
    _audit("categories", names, skipped=skipped, page_action=action)
    if skipped:
        return _done("bulk_manage.done.categories_skipped", count=deleted, skipped=skipped)
    return _done("bulk_manage.done.categories", count=deleted)


@bp.post("/admin/bulk/pages/delete")
@auth.admin_required
def delete_pages():
    if not _confirmed():
        return _refused()
    user = auth.current_user()
    deleted = handled = skipped = 0
    names: list[str] = []
    for page_id in _ids():
        page = pages.get(page_id)
        if page is None or page.get("is_home"):
            skipped += 1
            continue
        if registry.intercept("page.delete", page=page, user=user) is not None:
            handled += 1
            continue
        pages.delete(page, actor_id=user["id"])
        deleted += 1
        names.append(page["title"])
    _audit("pages", names, handled=handled, skipped=skipped)
    return _done("bulk_manage.done.pages", count=deleted, handled=handled, skipped=skipped)


@bp.post("/admin/bulk/canvases/delete")
@auth.admin_required
def delete_canvases():
    if not registry.is_enabled("canvas"):
        return auth.deny(404)
    if not _confirmed():
        return _refused()
    from ..canvas import service as canvas

    names: list[str] = []
    for layout_id in _ids():
        layout = db.one("SELECT * FROM canvas__layouts WHERE id = ?", (layout_id,))
        if layout:
            canvas.delete(layout)
            names.append(layout["title"])
    _audit("canvases", names)
    deleted = len(names)
    return _done("bulk_manage.done.canvases", count=deleted)


@bp.post("/admin/bulk/kanban-boards/delete")
@auth.admin_required
def delete_boards():
    if not registry.is_enabled("kanban"):
        return auth.deny(404)
    if not _confirmed():
        return _refused()
    from ..kanban import service as kanban

    names: list[str] = []
    for board_id in _ids():
        board = db.one("SELECT * FROM kanban_boards WHERE id = ?", (board_id,))
        if board:
            kanban.delete_board(board, auth.current_user())
            names.append(board["title"])
    _audit("boards", names)
    deleted = len(names)
    return _done("bulk_manage.done.boards", count=deleted)
