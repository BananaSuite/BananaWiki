"""Categories: a tree of folders for pages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ....core.timeutil import now_sql
from ... import auth
from ...db import db
from ...registry import emit, intercept
from . import service

MAX_NAME = 100


class CategoryError(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


class PagesRefused(CategoryError):
    """Deleting a category with its pages was refused; nothing was changed.

    ``reason`` is ``hidden`` (the category holds pages the user cannot see),
    ``forbidden`` (the user may not delete some of its pages) or ``blocked``
    (some pages are protected, checked out or already pending deletion);
    ``pages`` are the refused pages, all visible to the user. The message
    names the first few only: it travels in the session cookie as a flash.
    """

    TITLES_SHOWN = 10

    def __init__(self, reason: str, pages: list[dict[str, Any]] | None = None):
        self.reason = reason
        self.pages = pages or []
        values = {}
        if self.pages:
            titles = [page["title"] for page in self.pages[:self.TITLES_SHOWN]]
            if len(self.pages) > self.TITLES_SHOWN:
                titles.append(f"… (+{len(self.pages) - self.TITLES_SHOWN})")
            values["titles"] = ", ".join(titles)
        super().__init__(f"pages.error.category_pages_{reason}", **values)


@dataclass(frozen=True)
class Removal:
    """Pages :func:`delete_with_pages` left in place (slugs); they lose the category."""

    pending: tuple[str, ...] = ()  # scheduled for deletion instead (Deletion Slowdown)
    kept: tuple[str, ...] = ()  # kept by another feature's ``page.delete`` interceptor


def get(category_id: int | str | None) -> dict[str, Any] | None:
    try:
        category_id = int(category_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return db.one("SELECT * FROM categories WHERE id = ?", (category_id,))


def all_categories() -> list[dict[str, Any]]:
    return db.all("SELECT * FROM categories ORDER BY sort_order, name COLLATE NOCASE, id")


def ancestors(category_id: int | None) -> list[dict[str, Any]]:
    """Root-first chain of categories down to *category_id* (for breadcrumbs)."""
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current = get(category_id) if category_id else None
    while current and current["id"] not in seen:
        seen.add(current["id"])
        chain.append(current)
        current = get(current["parent_id"]) if current["parent_id"] else None
    return list(reversed(chain))


def is_descendant(category_id: int, ancestor_id: int) -> bool:
    return any(c["id"] == ancestor_id for c in ancestors(category_id)[:-1]) or category_id == ancestor_id


def listed(category_id: int | None, user: dict[str, Any] | None = None) -> bool:
    """Whether *user* sees *category_id* where categories are listed.

    The navigation, the category list of the API, category search results
    and subcategory lists need ``category.view_all`` on top of read access
    (administrators hold it; anonymous readers in public mode need none).
    Opening a readable category from a link needs read access only.
    """
    user = auth.current_user() if user is None else user
    if user is not None and not auth.has_permission("category.view_all", user):
        return False
    return auth.can_read_category(category_id, user)


def tree(user: dict[str, Any] | None = None, *, include_pages: bool = True) -> dict[str, Any]:
    """Navigation tree filtered for *user*.

    Returns ``{"categories": [node...], "uncategorized": [page...]}`` where a
    node is ``{id, name, sequential_nav, pages, children, page_count}``.
    A category the user may not list (:func:`listed`) is left out, but its
    listed descendants are lifted into its place (their names stay hidden).
    """
    user = auth.current_user() if user is None else user
    categories = all_categories()
    pages = service.list_visible(user=user, order="sort") if include_pages else []
    by_parent: dict[int | None, list[dict[str, Any]]] = {}
    for category in categories:
        by_parent.setdefault(category["parent_id"], []).append(category)
    pages_by_category: dict[int | None, list[dict[str, Any]]] = {}
    for page in pages:
        if page["is_home"]:
            continue
        pages_by_category.setdefault(page["category_id"], []).append(page)
    known = {c["id"] for c in categories}
    admin = auth.is_admin(user) if user else False

    def build(parent_id: int | None, trail: frozenset[int]) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for category in by_parent.get(parent_id, []):
            if category["id"] in trail:
                continue  # defensive: never loop on a corrupted parent chain
            children = build(category["id"], trail | {category["id"]})
            own_pages = pages_by_category.get(category["id"], [])
            if listed(category["id"], user):
                if not admin and not own_pages and not children and user is not None and not _is_writer(user):
                    continue
                nodes.append({
                    "id": category["id"], "name": category["name"], "sequential_nav": category["sequential_nav"],
                    "pages": own_pages, "children": children, "page_count": len(own_pages),
                })
            else:
                nodes.extend(children)
        return nodes

    roots = build(None, frozenset())
    # Categories whose parent no longer exists are shown at the top level.
    for orphan_parent in set(by_parent) - known - {None}:
        roots.extend(build(orphan_parent, frozenset()))
    return {"categories": roots, "uncategorized": pages_by_category.get(None, [])}


def _is_writer(user: dict[str, Any]) -> bool:
    return auth.has_role("editor", user)


def _clean_name(name: str | None) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise CategoryError("wiki.error.category_name_required")
    if len(name) > MAX_NAME:
        raise CategoryError("wiki.error.category_name_too_long", limit=MAX_NAME)
    return name


def create(name: str, parent_id: int | None = None, *, actor_id: str | None = None) -> dict[str, Any]:
    name = _clean_name(name)
    with db.transaction():
        if parent_id is not None and not get(parent_id):
            raise CategoryError("wiki.error.category_missing")
        order = int(db.scalar(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM categories WHERE parent_id IS ?", (parent_id,), default=0
        ))
        category_id = db.insert("categories", {
            "name": name, "parent_id": parent_id, "sort_order": order, "created_at": now_sql(),
        })
    category = get(category_id)
    assert category is not None
    emit("category.created", category=category, actor_id=actor_id)
    return category


def rename(category: dict[str, Any], name: str) -> dict[str, Any]:
    db.execute("UPDATE categories SET name = ? WHERE id = ?", (_clean_name(name), category["id"]))
    return get(category["id"])  # type: ignore[return-value]


def set_parent(category: dict[str, Any], parent_id: int | None) -> dict[str, Any]:
    with db.transaction():
        if parent_id is not None:
            if not get(parent_id):
                raise CategoryError("wiki.error.category_missing")
            if parent_id == category["id"] or is_descendant(parent_id, category["id"]):
                raise CategoryError("wiki.error.category_cycle")
        order = int(db.scalar(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM categories WHERE parent_id IS ?", (parent_id,), default=0
        ))
        db.execute("UPDATE categories SET parent_id = ?, sort_order = ? WHERE id = ?",
                   (parent_id, order, category["id"]))
    return get(category["id"])  # type: ignore[return-value]


def set_sequential(category: dict[str, Any], enabled: bool) -> None:
    db.execute("UPDATE categories SET sequential_nav = ? WHERE id = ?", (1 if enabled else 0, category["id"]))


def _reorder(table: str, group_column: str, group_value: int | None, ordered_ids: list[int], extra: str = "") -> None:
    """Put *ordered_ids* in the given order inside their group.

    Rows of the group that are not listed (not loaded by the client, or not
    visible to it) keep their slots, so a partial list never pushes them
    around or creates duplicate positions. Ids outside the group are ignored.
    """
    wanted = [int(item) for item in ordered_ids]
    with db.transaction():
        rows = db.all(
            f"SELECT id, sort_order FROM {table} WHERE {group_column} IS ?{extra} ORDER BY sort_order, id",
            (group_value,),
        )
        present = {row["id"] for row in rows}
        replacement = iter([item for item in wanted if item in present])
        selected = set(wanted) & present
        order = [next(replacement) if row["id"] in selected else row["id"] for row in rows]
        previous = {row["id"]: row["sort_order"] for row in rows}
        db.executemany(
            f"UPDATE {table} SET sort_order = ? WHERE id = ?",
            [(position, item) for position, item in enumerate(order) if previous[item] != position],
        )


def reorder_categories(ordered_ids: list[int], parent_id: int | None = None) -> None:
    _reorder("categories", "parent_id", parent_id, ordered_ids)


def reorder_pages(ordered_ids: list[int], category_id: int | None) -> None:
    _reorder("pages", "category_id", category_id, ordered_ids, " AND is_home = 0")


def search(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Categories whose name contains *query* (callers filter by read access)."""
    query = (query or "").strip()[:100]
    if not query:
        return []
    like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return db.all(
        "SELECT id, name, parent_id FROM categories WHERE name LIKE ? ESCAPE '\\' ORDER BY name COLLATE NOCASE LIMIT ?",
        (like, limit),
    )


def paths(readable: Callable[[int], bool] | None = None) -> dict[int, str]:
    """``{category_id: "Parent / Child"}`` for every category.

    With *readable*, ancestors it rejects are left out of the path, so the
    names of categories a reader cannot open never leak through a child.
    """
    rows = all_categories()
    by_id = {row["id"]: row for row in rows}
    result: dict[int, str] = {}
    for row in rows:
        names: list[str] = []
        seen: set[int] = set()
        current: dict[str, Any] | None = row
        while current is not None and current["id"] not in seen:
            seen.add(current["id"])
            if readable is None or current is row or readable(current["id"]):
                names.append(current["name"])
            current = by_id.get(current["parent_id"]) if current["parent_id"] else None
        result[row["id"]] = " / ".join(reversed(names))
    return result


def delete(category: dict[str, Any], *, page_action: str = "uncategorize", target_id: int | None = None,
           actor_id: str | None = None) -> None:
    """Delete a category. Its pages are uncategorised, moved or deleted; children move up to its parent.

    ``page_action="delete"`` deletes the pages without any permission check
    or interceptor, for the system's own use (replacing the built-in guide);
    deletions on behalf of someone go through :func:`delete_with_pages`.
    """
    if page_action not in ("uncategorize", "move", "delete"):
        raise CategoryError("wiki.error.invalid_action")
    if page_action == "delete":
        # Pages first: if one fails, the category stays with the pages left.
        for page in db.all("SELECT * FROM pages WHERE category_id = ? AND is_home = 0", (category["id"],)):
            service.delete(page, actor_id=actor_id)
    with db.transaction():
        if page_action == "move":
            if target_id is None or not get(target_id) or int(target_id) == category["id"]:
                raise CategoryError("wiki.error.category_missing")
            db.execute("UPDATE pages SET category_id = ? WHERE category_id = ?", (int(target_id), category["id"]))
        else:
            db.execute("UPDATE pages SET category_id = NULL WHERE category_id = ?", (category["id"],))
        db.execute("UPDATE categories SET parent_id = ? WHERE parent_id = ?", (category["parent_id"], category["id"]))
        db.execute("DELETE FROM categories WHERE id = ?", (category["id"],))
    emit("category.deleted", category=category, actor_id=actor_id)


def delete_with_pages(category: dict[str, Any], user: dict[str, Any], *, page_action: str = "uncategorize",
                      target_id: int | None = None) -> Removal:
    """Delete *category* for *user*: the one path of the web interface, the API and bulk deletion.

    With ``page_action="delete"`` every page goes as if *user* deleted it on
    its own. All of them must be visible to *user*, deletable
    (:func:`service.can_delete`) and not blocked (:func:`access.edit_blocked`);
    otherwise :class:`PagesRefused` is raised before anything changes. Each
    page then passes the ``page.delete`` interceptors, so Deletion Slowdown
    schedules it instead; pages that stay (scheduled, or kept by a feature)
    and the home page lose the category, which is deleted last.
    """
    if page_action != "delete":
        delete(category, page_action=page_action, target_id=target_id, actor_id=user["id"])
        return Removal()
    from . import access  # access imports this module

    pages = db.all("SELECT * FROM pages WHERE category_id = ? AND is_home = 0 ORDER BY id", (category["id"],))
    if not all(service.can_view(page, user) for page in pages):
        raise PagesRefused("hidden")
    forbidden = [page for page in pages if not service.can_delete(page, user)]
    if forbidden:
        raise PagesRefused("forbidden", forbidden)
    blocked = [page for page in pages if access.edit_blocked(page, user)]
    if blocked:
        raise PagesRefused("blocked", blocked)
    pending: list[str] = []
    kept: list[str] = []
    for page in pages:
        if intercept("page.delete", page=page, user=user) is None:
            service.delete(page, actor_id=user["id"])
            continue
        after = service.get(page["id"], with_content=False)
        if after is not None:
            (pending if after["pending_deletion"] else kept).append(page["slug"])
    delete(category, page_action="uncategorize", actor_id=user["id"])
    return Removal(tuple(pending), tuple(kept))


def count_pages(category_id: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM pages WHERE category_id = ?", (category_id,), default=0))
