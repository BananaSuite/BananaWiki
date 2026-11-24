"""Schema version 21: easter eggs administrators design themselves, and who found them.

* ``site_settings.easter_egg_configs``: the wiki's key-sequence easter eggs in
  the BananaSuite format ``banana-eggs/v1`` (:mod:`bananawiki.core.eggs`).
  NULL means "never saved": the default egg (the Konami code) applies.
* ``easter_egg_finds``: the first time each account found each egg.

BananaWiki 1.4 kept its eggs as a JSON *list* in ``site_settings.easter_eggs``
(the column the mascot's switches reuse as a JSON object since version 15) or,
before that, in the ``easter_egg_*`` columns, and who had found the egg in
``users.easter_egg_found``. They are imported here once, while
``easter_egg_configs`` is still NULL: the eggs become ``legacy-1``,
``legacy-2``, ... (:func:`bananawiki.core.eggs.from_legacy`), switched off if
the 1.4 ``easter_egg`` plugin was, and each account that had found the egg
gets a find of ``legacy-1``. The 1.4 values stay where they are; the mascot's
switches only replace a JSON list once it has been imported.

The DDL is frozen here: later schema changes belong in later versions.
"""

from __future__ import annotations

import json
import sqlite3

from ...core import eggs
from ...core.sqlite import add_columns, column_names, table_exists, tuples

LEGACY_COLUMNS = ("easter_egg_keys", "easter_egg_emoji", "easter_egg_count", "easter_egg_speed_min",
                  "easter_egg_speed_max", "easter_egg_delay", "easter_egg_unlock_delay")


def upgrade(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "site_settings"):
        return
    add_columns(conn, "site_settings", {"easter_egg_configs": "TEXT"})
    conn.execute(
        "CREATE TABLE IF NOT EXISTS easter_egg_finds ("
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "egg_id TEXT NOT NULL, "
        "found_at TEXT NOT NULL, "
        "PRIMARY KEY (user_id, egg_id))"
    )
    import_legacy(conn)


def _legacy_config(conn: sqlite3.Connection) -> dict | None:
    """The 1.4 eggs as ``banana-eggs/v1``: an empty list when 1.4 had none, None when there is nothing to import."""
    columns = column_names(conn, "site_settings")
    wanted = [name for name in ("easter_eggs", *LEGACY_COLUMNS) if name in columns]
    if not wanted:
        return None
    row = tuples(conn, "SELECT " + ", ".join(f'"{name}"' for name in wanted) + " FROM site_settings WHERE id = 1")
    if not row:
        return None
    values = dict(zip(wanted, row[0], strict=True))
    stored = values.pop("easter_eggs", None)
    try:
        parsed = json.loads(stored) if isinstance(stored, str) and stored.strip() else None
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        # 1.4 read this list first: "[]" meant no eggs, whatever the older columns held.
        return eggs.from_legacy(parsed) or {"version": eggs.VERSION, "eggs": []}
    if any(values.get(name) not in (None, "") for name in LEGACY_COLUMNS):
        return eggs.from_legacy(None, values)
    return None


def import_legacy(conn: sqlite3.Connection) -> None:
    current = tuples(conn, "SELECT easter_egg_configs FROM site_settings WHERE id = 1")
    if not current or current[0][0] is not None:
        return  # no settings row yet, or already imported (or saved since)
    config = _legacy_config(conn)
    if config is not None:
        if table_exists(conn, "plugins"):
            switched_off = tuples(conn, "SELECT 1 FROM plugins WHERE id = 'easter_egg' AND enabled = 0")
            if switched_off:
                config = {**config, "eggs": [{**egg, "enabled": False} for egg in config["eggs"]]}
        conn.execute("UPDATE site_settings SET easter_egg_configs = ? WHERE id = 1", (eggs.dumps(config),))
    if table_exists(conn, "users") and "easter_egg_found" in column_names(conn, "users"):
        ids = [egg["id"] for egg in (config or {}).get("eggs", [])]
        egg_id = "legacy-1" if "legacy-1" in ids else "konami"  # without imported eggs: the default egg
        conn.execute(
            "INSERT OR IGNORE INTO easter_egg_finds (user_id, egg_id, found_at) "
            "SELECT id, ?, datetime('now') FROM users WHERE easter_egg_found = 1",
            (egg_id,),
        )
