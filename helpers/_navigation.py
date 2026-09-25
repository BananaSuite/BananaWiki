"""Permission-aware sidebar queries with bounded page rows and keyset paging."""

from flask import g

import config
import db
from ._auth import is_public_mode_active, user_can_view_category
from ._request_cache import get_request_list_categories

INITIAL_PAGE_LIMIT = 100
PAGE_BATCH_SIZE = 50
PAGE_COLUMNS = (
    "id, title, slug, category_id, sort_order, is_home, is_deindexed, "
    "pending_deletion, builder_public, (builder_json != '') AS has_builder"
)


def _visibility(user):
    """Match user_can_view_page without loading page bodies into Python."""
    clauses, values = ["is_home=0"], []
    if not user:
        if not is_public_mode_active():
            return "0", []
        clauses.extend(["pending_deletion=0", "is_deindexed=0"])
        clauses.append("builder_json=''" if getattr(config, "FORBID_PUBLIC_BUILDER_PAGES", False)
                       else "(builder_json='' OR builder_public=1)")
    elif user["role"] not in ("admin", "owner"):
        categories = get_request_list_categories()
        allowed = [cat["id"] for cat in categories if user_can_view_category(user, cat["id"])]
        uncat = user_can_view_category(user, None)
        if len(allowed) != len(categories) or not uncat:
            # Category IDs are database integers. Bind the list as JSON to avoid
            # SQLite's variable-count limit for large custom permission sets.
            import json
            category_clause = "category_id IN (SELECT value FROM json_each(?))"
            values.append(json.dumps(allowed))
            clauses.append("(" + category_clause + (" OR category_id IS NULL)" if uncat else ")"))
        may_delete = db.has_permission(user, "page.delete")
        may_deindexed = db.has_permission(user, "page.view_deindexed")
        if not may_delete:
            clauses.append("pending_deletion=0")
        if not may_deindexed:
            clauses.append("(pending_deletion=1 OR is_deindexed=0)" if may_delete else "is_deindexed=0")
    return " AND ".join(clauses), values


def sidebar_navigation(user):
    """Return category metadata and at most 100 initial page links."""
    cached = getattr(g, "_bounded_sidebar_navigation", None)
    if cached is not None:
        return cached
    result = {"categories": [], "uncategorized": [], "page_ids": set(), "more": {}}
    if not user and not is_public_mode_active():
        return result
    where, values = _visibility(user)
    with db.get_db_context() as connection:
        counts = {row["category_id"]: row["n"] for row in connection.execute(
            f"SELECT category_id, COUNT(*) AS n FROM pages WHERE {where} GROUP BY category_id", values,
        )}
        pages = [dict(row) for row in connection.execute(
            f"SELECT {PAGE_COLUMNS} FROM pages WHERE {where} ORDER BY sort_order, title, id LIMIT ?",
            [*values, INITIAL_PAGE_LIMIT],
        )]
    nodes = {
        cat["id"]: {**dict(cat), "children": [], "pages": [], "page_count": counts.get(cat["id"], 0)}
        for cat in get_request_list_categories()
    }
    grouped = {None: result["uncategorized"], **{key: node["pages"] for key, node in nodes.items()}}
    for page in pages:
        if page["category_id"] in grouped:
            grouped[page["category_id"]].append(page)
            result["page_ids"].add(page["id"])
    for key, count in counts.items():
        loaded = grouped.get(key, [])
        if len(loaded) < count:
            result["more"][key] = loaded[-1]["id"] if loaded else 0
    for _key, node in nodes.items():
        parent = nodes.get(node["parent_id"])
        if parent is not None and parent is not node:
            parent["children"].append(node)
        else:
            result["categories"].append(node)
    # Preserve the existing rule that accessible descendants of a restricted
    # parent remain reachable without exposing that parent's name.
    from ._auth import filter_visible_navigation
    result.update(filter_visible_navigation(result["categories"], result["uncategorized"], user))
    g._bounded_sidebar_navigation = result
    return result


def navigation_page_batch(user, category_id, after=0):
    """Return at most 50 visible links and a cursor derived from database rows."""
    where, values = _visibility(user)
    where += " AND category_id IS ?"
    values.append(category_id)
    with db.get_db_context() as connection:
        if after:
            anchor = connection.execute(
                f"SELECT sort_order, title, id FROM pages WHERE {where} AND id=?", [*values, after],
            ).fetchone()
            if anchor is None:
                raise ValueError("Navigation changed. Reload the page and try again.")
            where += " AND (sort_order, title, id) > (?, ?, ?)"
            values.extend((anchor["sort_order"], anchor["title"], anchor["id"]))
        rows = [dict(row) for row in connection.execute(
            f"SELECT {PAGE_COLUMNS} FROM pages WHERE {where} ORDER BY sort_order, title, id LIMIT ?",
            [*values, PAGE_BATCH_SIZE + 1],
        )]
    pages = rows[:PAGE_BATCH_SIZE]
    return {"pages": pages, "after": pages[-1]["id"] if len(rows) > PAGE_BATCH_SIZE else None}
