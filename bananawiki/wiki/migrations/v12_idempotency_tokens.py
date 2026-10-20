"""Schema version 12: every idempotency key bound to the token that used it,
page history indexed by date and the new default colours.

Version 4 creates ``api_service__idempotency.token_id``, except on a database
that reached version 4 under the first 1.6.0 build, whose version 4 did not
have it yet. Such a database gets the column here. Rows without a token are
dropped, as version 4 drops them: they are at most a day of stored answers,
may hold secrets and belong to no token.

``idx_page_history_created`` serves the newest edits and the edits of the
past week without reading every revision.

The default colours changed from blue on navy to BananaChat's yellow on
charcoal. ``site_settings`` stores every colour, so a wiki that never changed
its colours still holds the old defaults: a theme (dark or light) whose six
colours are all still the old defaults takes the new ones. A theme with any
colour an administrator chose keeps all of its colours.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, table_exists

# Column prefix -> {colour: (old default, new default)}.
COLOURS = {
    "": {"primary": ("#8fa0d4", "#e6be32"), "secondary": ("#1e1e2c", "#1d1d1d"), "accent": ("#7e9ada", "#cda624"),
         "text": ("#c8ccd8", "#ededed"), "sidebar": ("#1a1a24", "#181818"), "bg": ("#16161f", "#141414")},
    "light_": {"primary": ("#4b63b6", "#8a6500"), "secondary": ("#ffffff", "#ffffff"),
               "accent": ("#3553c7", "#6f5000"), "text": ("#202534", "#202124"),
               "sidebar": ("#e9edf5", "#f4f1e8"), "bg": ("#f6f7fb", "#faf9f5")},
}


def upgrade(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "api_service__idempotency"):
        add_columns(conn, "api_service__idempotency", {"token_id": "INTEGER"})
        conn.execute("DELETE FROM api_service__idempotency WHERE token_id IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_history_created ON page_history(created_at)")
    for prefix, colours in COLOURS.items():
        columns = [f"{prefix}{name}_color" for name in colours]
        assignments = ", ".join(f"{column} = ?" for column in columns)
        unchanged = " AND ".join(f"lower({column}) = ?" for column in columns)
        conn.execute(f"UPDATE site_settings SET {assignments} WHERE {unchanged}",  # noqa: S608 - fixed names
                     [new for _old, new in colours.values()] + [old for old, _new in colours.values()])
