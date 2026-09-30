"""Schema additions of custom pages."""

from __future__ import annotations

import sqlite3

from ....core.sqlite import add_columns


def upgrade_v4(conn: sqlite3.Connection) -> None:
    """Pages made with the visual builder keep their document next to its Markdown twin.

    Such a page is stored as a ``wiki_page`` whose ``content`` is the Markdown
    version of the document, so a release without this column still shows it.
    """
    add_columns(conn, "custom_pages", {"builder_json": "TEXT NOT NULL DEFAULT ''"})
