"""The navigation tree (sidebar and navigation page) with bounded page loading.

A wiki can hold thousands of pages; the sidebar loads at most
``INITIAL_PER_CATEGORY`` links per category (``INITIAL_TOTAL`` overall) and
the reader loads more in batches of ``BATCH_SIZE``. Visibility is applied in
SQL through :func:`service.visible_filter`, and category names the reader
may not list (:func:`categories.listed`) are never shown: their listed
descendants move up instead.
"""

from __future__ import annotations

from typing import Any

from flask import g

from ... import auth
from ...db import db
from . import categories, service

INITIAL_PER_CATEGORY = 25
INITIAL_TOTAL = 400
BATCH_SIZE = 50
_COLUMNS = "p.id, p.title, p.slug, p.category_id, p.sort_order, p.is_deindexed, p.pending_deletion"
_ORDER = "p.sort_order, lower(p.title), p.id"


class NavigationChanged(ValueError):
    """The page a batch should continue after is gone or no longer visible."""


def _visible(user: dict[str, Any] | None) -> tuple[str, list[Any]]:
    where, params = service.visible_filter(user)
    return f"({where}) AND p.is_home = 0", list(params)


def tree(user: dict[str, Any] | None = None, *, per_category: int = INITIAL_PER_CATEGORY,
         total: int = INITIAL_TOTAL) -> dict[str, Any]:
    """Category nodes with their first pages.

    Returns ``{"categories": [node], "uncategorized": {pages, count, more}}``;
    a node is ``{id, name, parent_id, sequential_nav, pages, count, more, children}``
    where ``more`` is the id to continue after, or None when every page is loaded.
    """
    user = auth.current_user() if user is None else user
    cache_key = (per_category, total)
    cached = g.get("_pages_nav_tree")
    if cached is not None and cached[0] == cache_key and cached[1] is user:
        return cached[2]
    where, params = _visible(user)
    counts = {row["category_id"]: row["n"] for row in db.all(
        f"SELECT p.category_id, COUNT(*) AS n FROM pages p WHERE {where} GROUP BY p.category_id", params)}
    rows = db.all(
        f"SELECT * FROM (SELECT {_COLUMNS}, ROW_NUMBER() OVER (PARTITION BY p.category_id ORDER BY {_ORDER}) AS rn "
        f"FROM pages p WHERE {where}) WHERE rn <= ? ORDER BY rn LIMIT ?",
        [*params, per_category, total],
    )
    rows.sort(key=lambda r: (r["sort_order"], r["title"].lower(), r["id"]))
    grouped: dict[int | None, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["category_id"], []).append(row)

    def section(key: int | None) -> dict[str, Any]:
        loaded = grouped.get(key, [])
        count = counts.get(key, 0)
        more = loaded[-1]["id"] if loaded and len(loaded) < count else (0 if count and not loaded else None)
        return {"pages": loaded, "count": count, "more": more}

    result = {
        "categories": _category_nodes(user, counts, section),
        "uncategorized": section(None),
    }
    g._pages_nav_tree = (cache_key, user, result)
    return result


def _category_nodes(user, counts, section) -> list[dict[str, Any]]:
    all_rows = categories.all_categories()
    by_parent: dict[int | None, list[dict[str, Any]]] = {}
    known = {c["id"] for c in all_rows}
    for category in all_rows:
        parent = category["parent_id"] if category["parent_id"] in known else None
        by_parent.setdefault(parent, []).append(category)
    shows_empty = bool(user) and auth.has_role("editor", user)

    def build(parent_id: int | None, trail: frozenset[int]) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for category in by_parent.get(parent_id, []):
            if category["id"] in trail:
                continue
            children = build(category["id"], trail | {category["id"]})
            if not categories.listed(category["id"], user):
                nodes.extend(children)
                continue
            data = section(category["id"])
            if not shows_empty and not data["count"] and not children:
                continue
            nodes.append({
                "id": category["id"], "name": category["name"], "parent_id": category["parent_id"],
                "sequential_nav": category["sequential_nav"], "children": children, **data,
            })
        return nodes

    return build(None, frozenset())


def batch(user: dict[str, Any] | None, category_id: int | None, after: int = 0,
          limit: int = BATCH_SIZE) -> dict[str, Any]:
    """The next pages of one category after page *after*: ``{pages, more}``."""
    where, params = _visible(user)
    where += " AND p.category_id IS ?"
    params.append(category_id)
    if after:
        anchor = db.one(f"SELECT p.sort_order, lower(p.title) AS t, p.id FROM pages p WHERE {where} AND p.id = ?",
                        [*params, after])
        if anchor is None:
            raise NavigationChanged(after)
        where += " AND (p.sort_order, lower(p.title), p.id) > (?, ?, ?)"
        params.extend((anchor["sort_order"], anchor["t"], anchor["id"]))
    rows = db.all(f"SELECT {_COLUMNS} FROM pages p WHERE {where} ORDER BY {_ORDER} LIMIT ?", [*params, limit + 1])
    pages = rows[:limit]
    return {"pages": pages, "more": pages[-1]["id"] if len(rows) > limit else None}


def page_ids(nav: dict[str, Any]) -> set[int]:
    """Ids of every page link present in a tree."""
    found = {p["id"] for p in nav["uncategorized"]["pages"]}
    stack = list(nav["categories"])
    while stack:
        node = stack.pop()
        found.update(p["id"] for p in node["pages"])
        stack.extend(node["children"])
    return found


def branch_ids(nav: dict[str, Any], category_id: int | None) -> set[int]:
    """Ids of the nodes on the way to *category_id* (to open them for the current page)."""
    if category_id is None:
        return set()

    def walk(nodes: list[dict[str, Any]], trail: list[int]) -> list[int] | None:
        for node in nodes:
            if node["id"] == category_id:
                return [*trail, node["id"]]
            found = walk(node["children"], [*trail, node["id"]])
            if found:
                return found
        return None

    return set(walk(nav["categories"], []) or [])
