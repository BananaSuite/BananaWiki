"""Schema version 17: the pages the documentation installer wrote (``builtin_doc_pages``).

With ``docs_bypass_deletion_slowdown`` on, these pages skip the grace period
of the deletion slowdown. Before, a page counted as one when its first
remaining history entry was by the system with the installer's message,
which an editor could build for any page (that summary, then removing the
attribution and the older entries). The installer now lists its pages here,
and only those skip the grace period.

The pages that meet the old rule in the current documentation category are
listed once, so the guide installed by an earlier release keeps skipping it.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import column_names, table_exists


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS builtin_doc_pages ("
        "page_id INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE)"
    )
    if not (table_exists(conn, "site_settings") and "docs_category_id" in column_names(conn, "site_settings")):
        return
    conn.execute(
        "INSERT OR IGNORE INTO builtin_doc_pages (page_id) "
        "SELECT p.id FROM pages p JOIN site_settings s ON s.id = 1 AND s.docs_category_id = p.category_id "
        "WHERE EXISTS (SELECT 1 FROM page_history h WHERE h.id = "
        "(SELECT MIN(id) FROM page_history WHERE page_id = p.id) "
        "AND h.edited_by IS NULL AND h.edit_message = 'Built-in documentation')"
    )
