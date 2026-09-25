"""Category CRUD, tree, and ordering."""

from ._connection import get_db_context, retry_on_busy


def create_category(name, parent_id=None):
    """Create a new category with the given *name* and optional *parent_id*.  Returns the new row ID."""
    with get_db_context() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO categories (name, parent_id) VALUES (?, ?)", (name, parent_id))
        cat_id = cur.lastrowid
        conn.commit()
        return cat_id


@retry_on_busy
def get_category(cat_id):
    """Return the category row for the given *cat_id*, or None if not found."""
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
        return row


def update_category(cat_id, name):
    """Rename the category identified by *cat_id* to *name*."""
    with get_db_context() as conn:
        conn.execute("UPDATE categories SET name=? WHERE id=?", (name, cat_id))
        conn.commit()


@retry_on_busy
def is_descendant_of(cat_id, ancestor_id):
    """Return True if ancestor_id is a descendant of cat_id (circular ref check)."""
    with get_db_context() as conn:
        current = ancestor_id
        visited = set()
        while current is not None:
            if current == cat_id:
                return True
            if current in visited:
                break
            visited.add(current)
            row = conn.execute("SELECT parent_id FROM categories WHERE id=?",
                               (current,)).fetchone()
            current = row["parent_id"] if row else None
        return False


def update_category_parent(cat_id, parent_id):
    """Move a category under a different parent (or to top level if parent_id is None)."""
    with get_db_context() as conn:
        conn.execute("UPDATE categories SET parent_id=? WHERE id=?", (parent_id, cat_id))
        conn.commit()


def delete_category(cat_id, page_action="uncategorize", target_category_id=None):
    """Delete a category and handle its pages.

    page_action:
        "uncategorize" - move pages to uncategorized (default, backward-compatible)
        "delete"       - delete all pages in this category
        "move"         - move pages to target_category_id
    """
    with get_db_context() as conn:
        if page_action == "delete":
            # Delete pages (and their history/drafts via CASCADE)
            conn.execute("DELETE FROM pages WHERE category_id=? AND is_home=0", (cat_id,))
            conn.execute("UPDATE pages SET category_id=NULL WHERE category_id=? AND is_home=1", (cat_id,))
        elif page_action == "move" and target_category_id:
            conn.execute("UPDATE pages SET category_id=? WHERE category_id=?",
                         (target_category_id, cat_id))
        else:
            conn.execute("UPDATE pages SET category_id=NULL WHERE category_id=?", (cat_id,))
        conn.execute("UPDATE categories SET parent_id=NULL WHERE parent_id=?", (cat_id,))
        conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
        conn.commit()


@retry_on_busy
def count_pages_in_category(cat_id):
    """Return the number of non-home pages in the given category."""
    with get_db_context() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM pages WHERE category_id=? AND is_home=0",
                           (cat_id,)).fetchone()[0]
        return cnt


@retry_on_busy
def list_categories():
    """Return all categories ordered by sort_order then name.

    Wrapped in :func:`retry_on_busy` because the sidebar category list
    is loaded on every page render via :func:`inject_globals`.  A
    transient ``database is locked`` here used to turn the whole page
    into a 500 even though the request itself was fine.
    """
    with get_db_context() as conn:
        rows = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
        return rows


@retry_on_busy
def get_category_tree(*, include_content=True):
    """Return nested category structure with pages.

    Wrapped in :func:`retry_on_busy` for the same reason as
    :func:`list_categories`: every page render queries this for the
    sidebar tree, so a single transient SQLite lock would otherwise
    surface as a 500 to the user.
    """
    cats = list_categories()
    # Navigation and permission checks do not need every page body or builder
    # document. Keep full rows available to data exporters and existing callers.
    columns = "*" if include_content else (
        "id, title, slug, category_id, sort_order, is_home, is_deindexed, "
        "pending_deletion, builder_public, (builder_json != '') AS has_builder"
    )
    with get_db_context() as conn:
        pages = conn.execute(
            f"SELECT {columns} FROM pages WHERE is_home=0 ORDER BY sort_order, title"
        ).fetchall()

    cat_map = {}
    for c in cats:
        cat_map[c["id"]] = {"id": c["id"], "name": c["name"], "parent_id": c["parent_id"],
                            "sequential_nav": c["sequential_nav"],
                            "children": [], "pages": []}

    for p in pages:
        if p["category_id"] and p["category_id"] in cat_map:
            cat_map[p["category_id"]]["pages"].append(dict(p))

    roots = []
    uncategorized_pages = [dict(p) for p in pages if not p["category_id"]]

    for _cid, cat in cat_map.items():
        if cat["parent_id"] and cat["parent_id"] in cat_map:
            cat_map[cat["parent_id"]]["children"].append(cat)
        else:
            roots.append(cat)

    return roots, uncategorized_pages


def update_pages_sort_order(ordered_ids):
    """Reorder selected pages in their existing slots, preserving omitted pages.

    A paged sidebar sends only loaded links. Reassigning those links from zero
    would move them past hidden or unloaded pages and introduce duplicate ranks.
    """
    ordered_ids = [int(page_id) for page_id in ordered_ids]
    if not ordered_ids:
        return
    if len(ordered_ids) > 10000 or len(set(ordered_ids)) != len(ordered_ids):
        raise ValueError("Choose distinct pages within the reorder limit.")
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in ordered_ids)
        requested = list(conn.execute(
            f"SELECT id, category_id, is_home FROM pages WHERE id IN ({placeholders})", ordered_ids,
        ))
        if len(requested) != len(ordered_ids):
            raise ValueError("The selected pages changed. Reload and try again.")
        # Older API clients include Home in their list. Accept it while keeping
        # its fixed navigation position separate from ordinary page ordering.
        selected = {row["id"]: row["category_id"] for row in requested if not row["is_home"]}
        for category_id in set(selected.values()):
            rows = list(conn.execute(
                "SELECT id, sort_order FROM pages WHERE is_home=0 AND category_id IS ? ORDER BY sort_order, title, id",
                (category_id,),
            ))
            replacement = iter(page_id for page_id in ordered_ids if page_id in selected and selected[page_id] == category_id)
            order = [next(replacement) if row["id"] in selected else row["id"] for row in rows]
            previous = {row["id"]: row["sort_order"] for row in rows}
            conn.executemany("UPDATE pages SET sort_order=? WHERE id=?",
                             [(position, page_id) for position, page_id in enumerate(order) if previous[page_id] != position])
        conn.commit()


def update_categories_sort_order(ordered_ids):
    """Assign sort_order 0, 1, 2, … to categories given in the specified order."""
    with get_db_context() as conn:
        for i, cat_id in enumerate(ordered_ids):
            conn.execute("UPDATE categories SET sort_order=? WHERE id=?", (i, cat_id))
        conn.commit()


@retry_on_busy
def search_categories(query, limit=10):
    """Return categories whose name matches *query* (case-insensitive substring).

    Returns a list of dicts with ``id``, ``name``, ``parent_id``.
    """
    from ._pages import _escape_like

    with get_db_context() as conn:
        pattern = f"%{_escape_like(query)}%"
        rows = conn.execute(
            "SELECT id, name, parent_id FROM categories WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return [dict(r) for r in rows]
