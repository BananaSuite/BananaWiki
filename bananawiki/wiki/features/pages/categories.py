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
# Levels of nesting (a top-level category is level 1). The navigation is
# rendered recursively, so a deeper chain would break it for every reader;
# older, deeper trees are shown cut off at this level (:func:`display_parents`).
MAX_DEPTH = 32
MAX_CATEGORIES = 10_000  # the sidebar lists every category
# Characters of a category path in the pickers ("… / Parent / Child"): every
# editor's page view lists every category's path, which would otherwise grow
# with the square of the depth. The category's own name is always kept.
MAX_PATH = 160


class CategoryError(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


class PagesRefused(CategoryError):
    """Deleting a category with its pages was refused; nothing was changed.

    ``reason`` is ``hidden`` (the category holds pages the user cannot see),
    ``forbidden`` (the user may not delete some of its pages), ``immovable``
    (the user may not change the category of some of its pages), ``blocked``
    (some pages are protected, checked out or already pending deletion),
    ``top_level`` (pages would be left without a category, where the user may
    not write) or ``subcategories`` (subcategories would move, but the user
    may not write them or their new parent); ``pages`` are the refused pages,
    all visible to the user. The message names the first few only: it
    travels in the session cookie as a flash.
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


def subtree_ids(category_id: int) -> set[int]:
    """*category_id* and every category below it (one query)."""
    children: dict[int, list[int]] = {}
    for row in db.all("SELECT id, parent_id FROM categories WHERE parent_id IS NOT NULL"):
        children.setdefault(row["parent_id"], []).append(row["id"])
    found = {category_id}
    pending = [category_id]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _levels(rows: list[dict[str, Any]]) -> dict[int, int]:
    """``{id: level}`` (top level 1) of every category on a chain from the top; loops are left out."""
    parents = {row["id"]: row["parent_id"] for row in rows}
    levels: dict[int, int] = {}
    looping: set[int] = set()
    for start in parents:
        chain: list[int] = []
        on_chain: set[int] = set()
        current: int | None = start
        while current is not None and current not in levels and current not in on_chain and current not in looping:
            chain.append(current)
            on_chain.add(current)
            parent = parents.get(current)
            current = parent if parent in parents else None
        if current is not None and current not in levels:
            looping.update(chain)  # a corrupted parent chain that loops: never reachable from the top
            continue
        level = levels[current] if current is not None else 0
        for category_id in reversed(chain):
            level += 1
            levels[category_id] = level
    return levels


def display_parents(rows: list[dict[str, Any]]) -> dict[int, int | None]:
    """``{id: parent_id}`` for building a tree no deeper than :data:`MAX_DEPTH`.

    A category below that level (left by an older version) is attached to its
    ancestor one level above the bound instead, so it still shows, at the
    deepest level. Other categories keep their parent.
    """
    levels = _levels(rows)
    parents = {row["id"]: row["parent_id"] for row in rows}
    anchors: dict[int, int] = {}  # the deepest ancestor-or-self above MAX_DEPTH

    def anchor(category_id: int) -> int:
        chain: list[int] = []
        current = category_id
        while current not in anchors and levels[current] >= MAX_DEPTH:
            chain.append(current)
            current = parents[current]  # type: ignore[assignment]  # level > 1, so a parent exists
        found = anchors.get(current, current)
        for item in chain:
            anchors[item] = found
        return found

    result: dict[int, int | None] = {}
    for row in rows:
        level = levels.get(row["id"])
        result[row["id"]] = anchor(row["parent_id"]) if level is not None and level > MAX_DEPTH else row["parent_id"]
    return result


def _depth_below(category_id: int) -> int:
    """Levels of *category_id*'s subtree, itself included (1 without subcategories)."""
    children: dict[int, list[int]] = {}
    for row in db.all("SELECT id, parent_id FROM categories WHERE parent_id IS NOT NULL"):
        children.setdefault(row["parent_id"], []).append(row["id"])
    height, level, seen = 0, [category_id], {category_id}
    while level:
        height += 1
        level = [child for item in level for child in children.get(item, ()) if child not in seen]
        seen.update(level)
    return height


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
    parents = display_parents(categories)
    for category in categories:
        by_parent.setdefault(parents[category["id"]], []).append(category)
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


def ensure_room(freed: int = 0) -> None:
    """Raise :class:`CategoryError` unless one more category fits once *freed* categories are gone."""
    if int(db.scalar("SELECT COUNT(*) FROM categories", default=0)) - freed >= MAX_CATEGORIES:
        raise CategoryError("wiki.error.category_too_many", limit=MAX_CATEGORIES)


def create(name: str, parent_id: int | None = None, *, actor_id: str | None = None) -> dict[str, Any]:
    name = _clean_name(name)
    with db.transaction():
        if parent_id is not None and not get(parent_id):
            raise CategoryError("wiki.error.category_missing")
        if parent_id is not None and len(ancestors(parent_id)) >= MAX_DEPTH:
            raise CategoryError("wiki.error.category_too_deep", limit=MAX_DEPTH)
        ensure_room()
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
            if len(ancestors(parent_id)) + _depth_below(category["id"]) > MAX_DEPTH:
                raise CategoryError("wiki.error.category_too_deep", limit=MAX_DEPTH)
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


def shorten_path(names: list[str], limit: int | None = MAX_PATH) -> str:
    """``"Parent / Child"`` from *names* (top first), at most about *limit* characters.

    The nearest ancestors that fit are kept after a leading ``"… / "``; the
    last name always stays whole. ``limit=None`` joins them all.
    """
    if limit is None:
        return " / ".join(names)
    kept, length = [], 0
    for name in reversed(names):
        if kept and length + 3 + len(name) > limit:
            kept.append("…")
            break
        kept.append(name)
        length += len(name) + (3 if len(kept) > 1 else 0)
    return " / ".join(reversed(kept))


def paths(readable: Callable[[int], bool] | None = None, *, limit: int | None = MAX_PATH) -> dict[int, str]:
    """``{category_id: "Parent / Child"}`` for every category.

    With *readable*, ancestors it rejects are left out of the path, so the
    names of categories a reader cannot open never leak through a child.
    Paths are shortened to *limit* characters (:func:`shorten_path`); search
    passes ``limit=None`` to match against every ancestor.
    """
    rows = all_categories()
    by_id = {row["id"]: row for row in rows}
    result: dict[int, str] = {}
    for row in rows:
        names: list[str] = []
        length = 0
        seen: set[int] = set()
        current: dict[str, Any] | None = row
        while current is not None and current["id"] not in seen:
            seen.add(current["id"])
            if readable is None or current is row or readable(current["id"]):
                names.append(current["name"])
                length += len(current["name"]) + 3
                if limit is not None and len(names) > 1 and length > limit + 3:
                    break  # shorten_path drops the rest
            current = by_id.get(current["parent_id"]) if current["parent_id"] else None
        result[row["id"]] = shorten_path(list(reversed(names)), limit)
    return result


def delete(category: dict[str, Any], *, page_action: str = "uncategorize", target_id: int | None = None,
           actor_id: str | None = None) -> None:
    """Delete a category. Its pages are uncategorised, moved or deleted; children move up to its parent.

    ``page_action="delete"`` deletes the pages without any permission check
    or interceptor, for the system's own use only;
    deletions on behalf of someone go through :func:`delete_with_pages`.
    """
    if page_action not in ("uncategorize", "move", "delete"):
        raise CategoryError("wiki.error.invalid_action")
    if page_action == "delete":
        # Pages first: if one fails, the category stays with the pages left.
        for page_id in db.column("SELECT id FROM pages WHERE category_id = ? AND is_home = 0", (category["id"],)):
            page = service.get(page_id)
            if page is not None:
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

    Pages that move (``uncategorize`` or ``move``) must all be visible to
    *user*, movable (:func:`access.can_edit_metadata`) and not blocked
    (:func:`access.edit_blocked`), as when moving each one on its own; pages left without a category need write access to the
    top level. Subcategories move up to the category's parent, so *user*
    must be able to write each of them and that parent. Otherwise
    :class:`PagesRefused` is raised before anything changes.

    With ``page_action="delete"`` every page goes as if *user* deleted it on
    its own. All of them must be visible to *user*, deletable
    (:func:`service.can_delete`) and not blocked; otherwise
    :class:`PagesRefused` is raised before anything changes. Each page then
    passes the ``page.delete`` interceptors, so Deletion Slowdown schedules it
    instead; pages that stay (scheduled, or kept by a feature) and the home
    page lose the category, which is deleted last.
    """
    from . import access  # access imports this module

    children = db.column("SELECT id FROM categories WHERE parent_id = ?", (category["id"],))
    if children and not all(auth.can_write_category(child, user)
                            for child in [*children, category["parent_id"]]):
        raise PagesRefused("subcategories")
    # The checks read the pages without their text, which a large category could not hold at once.
    if page_action != "delete":
        pages = db.all(f"SELECT {service.PAGE_COLUMNS} FROM pages WHERE category_id = ? ORDER BY id",
                       (category["id"],))
        if not all(service.can_view(page, user) for page in pages):
            raise PagesRefused("hidden")
        immovable = [page for page in pages if not access.can_edit_metadata(page, user)]
        if immovable:
            raise PagesRefused("immovable", immovable)
        blocked = [page for page in pages if access.edit_blocked(page, user)]
        if blocked:
            raise PagesRefused("blocked", blocked)
        if pages and page_action == "uncategorize" and not auth.can_write_category(None, user):
            raise PagesRefused("top_level")
        delete(category, page_action=page_action, target_id=target_id, actor_id=user["id"])
        return Removal()
    pages = db.all(f"SELECT {service.PAGE_COLUMNS} FROM pages WHERE category_id = ? AND is_home = 0 ORDER BY id",
                   (category["id"],))
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
    for listed in pages:
        page = service.get(listed["id"])  # with its text, for the interceptors and handlers, one at a time
        if page is None:
            continue
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
