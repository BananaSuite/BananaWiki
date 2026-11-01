"""Schema version 18: the page slugs that canvases' wiki-page nodes store.

Wiki-page nodes from older versions, and those naming a page their writer
could not read, link their page by slug instead of id. When a page is
renamed, retitled, deleted or restored, :mod:`bananawiki.wiki.features.canvas.service`
points those nodes at the page by id. ``canvas__page_slugs`` lists, for each
canvas, the slugs its wiki-page nodes store, so a page event reads only the
canvases that link that page: without it, every canvas whose text merely
mentioned the slug had to be parsed while the page's transaction waited.

Triggers keep the list in step with ``canvas__layouts.data`` whoever writes
it, and deleting a canvas deletes its rows. A document SQLite cannot read as
JSON lists no slugs, as the service reads it as an empty canvas.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import table_exists

#: (canvas id, slug) for each wiki-page node storing a slug, in canvas row ``{layout}`` (of ``{source}``).
_SLUGS = (
    "SELECT DISTINCT {layout}.id, substr(CAST(json_extract(n.value, '$.page_slug') AS TEXT), 1, 200) "
    "FROM {source}json_each(CASE WHEN json_valid({layout}.data) AND json_type({layout}.data, '$.nodes') = 'array' "
    "THEN {layout}.data ELSE '[]' END, '$.nodes') AS n "
    "WHERE n.type = 'object' AND lower(trim(json_extract(n.value, '$.type'))) IN ('wiki_page', 'page') "
    "AND json_type(n.value, '$.page_slug') IN ('text', 'integer') "
    "AND length(CAST(json_extract(n.value, '$.page_slug') AS TEXT)) > 0"
)
_NEW_SLUGS = _SLUGS.format(layout="new", source="")
_ALL_SLUGS = _SLUGS.format(layout="l", source="canvas__layouts AS l, ")


def upgrade(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "canvas__layouts"):
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS canvas__page_slugs ("
        "layout_id INTEGER NOT NULL REFERENCES canvas__layouts(id) ON DELETE CASCADE, slug TEXT NOT NULL, "
        "PRIMARY KEY (slug, layout_id)) WITHOUT ROWID"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_canvas_page_slugs_layout ON canvas__page_slugs(layout_id)")
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS canvas_page_slugs_insert AFTER INSERT ON canvas__layouts BEGIN "
        f"INSERT OR IGNORE INTO canvas__page_slugs (layout_id, slug) {_NEW_SLUGS}; END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS canvas_page_slugs_update AFTER UPDATE OF data ON canvas__layouts BEGIN "
        "DELETE FROM canvas__page_slugs WHERE layout_id = old.id; "
        f"INSERT OR IGNORE INTO canvas__page_slugs (layout_id, slug) {_NEW_SLUGS}; END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS canvas_page_slugs_delete AFTER DELETE ON canvas__layouts BEGIN "
        "DELETE FROM canvas__page_slugs WHERE layout_id = old.id; END"
    )
    conn.execute(f"INSERT OR IGNORE INTO canvas__page_slugs (layout_id, slug) {_ALL_SLUGS}")
