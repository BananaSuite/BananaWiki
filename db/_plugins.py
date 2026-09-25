"""Plugin registry: CRUD operations for the ``plugins`` table."""

import config
from sqlite_runtime import boot_lock

from ._connection import get_db_context, retry_on_busy


@retry_on_busy
def list_plugins():
    """Return all registered plugins ordered by name.

    Wrapped in :func:`retry_on_busy` because the plugin registry is read
    on every request via ``_get_request_enabled_plugins`` (path-based
    plugin gating in ``before_request_hook``).  A transient ``database
    is locked`` here used to bubble up as the "random 500 that goes away
    after a reload" pattern users observe under concurrent writers.
    """
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM plugins ORDER BY builtin DESC, name"
        ).fetchall()


@retry_on_busy
def get_plugin(plugin_id):
    """Return a single plugin row or ``None``."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM plugins WHERE id = ?", (plugin_id,)
        ).fetchone()


def register_plugin(plugin_id, *, name, version, author="", description="",
                    builtin=False, enabled=False, update_existing=True):
    """Insert or explicitly refresh a plugin row.

    Returns ``True`` if a new row was inserted, ``False`` if an existing row was
    updated or preserved.  Startup seeding passes ``update_existing=False`` so
    it does not overwrite an administrator's enabled/disabled choice.
    """
    with get_db_context() as conn:
        existing = conn.execute(
            "SELECT id FROM plugins WHERE id = ?", (plugin_id,)
        ).fetchone()
        if existing:
            if update_existing:
                conn.execute(
                    "UPDATE plugins SET name=?, version=?, author=?, description=?, "
                    "builtin=?, enabled=? WHERE id=?",
                    (
                        name,
                        version,
                        author,
                        description,
                        1 if builtin else 0,
                        1 if enabled else 0,
                        plugin_id,
                    ),
                )
                conn.commit()
            return False
        # Several processes can seed the built-in plugins at once, for example
        # two Gunicorn workers booting together.  Let the loser of that race
        # keep the winner's row instead of raising on the unique constraint.
        # installed_at is written to the millisecond, not left to the column
        # default of whole seconds: a worker compares it with the row its
        # plugin code was loaded for, and a plugin deleted and installed again
        # within one second must still count as a new installation.
        cursor = conn.execute(
            "INSERT INTO plugins (id, name, version, author, description, builtin, enabled, "
            "installed_at) VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now')) "
            "ON CONFLICT(id) DO NOTHING",
            (plugin_id, name, version, author, description,
             1 if builtin else 0, 1 if enabled else 0),
        )
        conn.commit()
        return cursor.rowcount > 0


def sync_plugin_origins(builtin_manifests, external_ids):
    """Make the ``builtin`` column match where each plugin's code lives.

    The loader decides what is built-in from the folder a plugin was found
    in, never from this column: an enabled plugin can write to the database
    and could otherwise mark itself built-in.  The column still drives what
    the admin pages show, so it is corrected here at every start.

    A built-in row that had been flipped to external (by an upload that
    reused the id, before uploads were stopped from doing that) also gets
    its name, version, author and description back from the shipped
    manifest.  Its enabled flag is left as it is.
    """
    external_ids = [plugin_id for plugin_id in external_ids if plugin_id]
    with get_db_context() as conn:
        for manifest in builtin_manifests:
            plugin_id = manifest["id"]
            conn.execute(
                "UPDATE plugins SET builtin=1, name=?, version=?, author=?, "
                "description=? WHERE id=? AND builtin=0",
                (
                    manifest.get("name", plugin_id),
                    manifest.get("version", "0.0.0"),
                    manifest.get("author", "BananaWiki"),
                    manifest.get("description", ""),
                    plugin_id,
                ),
            )
        if external_ids:
            placeholders = ",".join("?" for _ in external_ids)
            conn.execute(
                f"UPDATE plugins SET builtin=0 WHERE builtin=1 AND id IN ({placeholders})",  # noqa: S608
                external_ids,
            )
        conn.commit()


def update_plugin(plugin_id, **kwargs):
    """Update mutable columns on a plugin row.

    Accepted keyword arguments: ``name``, ``version``, ``author``,
    ``description``, ``enabled``, ``enabled_at``, ``disabled_at``.
    """
    _ALLOWED = {
        "name", "version", "author", "description",
        "enabled", "enabled_at", "disabled_at",
    }
    updates = {k: v for k, v in kwargs.items() if k in _ALLOWED}
    if not updates:
        return
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [plugin_id]
    with get_db_context() as conn:
        conn.execute(
            f"UPDATE plugins SET {set_clause} WHERE id = ?",   # noqa: S608
            values,
        )
        conn.commit()


def enable_plugin(plugin_id):
    """Mark a plugin as enabled and record the timestamp."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    update_plugin(plugin_id, enabled=1, enabled_at=now)


def set_plugins_enabled(plugin_ids, enabled=True):
    """Enable or disable a batch of plugin ids.

    Only built-in rows are enabled here; disabling works for any row.
    Enabling an uploaded plugin runs third-party code, so it goes through
    the plugin admin page, which asks for the admin's password and copies
    the database first.  A batch toggle such as the onboarding form must not
    be a way around that.  The ``builtin`` column is reliable enough for
    this: it is set from each plugin's folder at every start, and an upload
    is always registered as external.
    """
    from datetime import datetime, timezone
    if not plugin_ids:
        return
    value = 1 if enabled else 0
    timestamp_column = "enabled_at" if enabled else "disabled_at"
    only_builtin = " AND builtin = 1" if enabled else ""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    placeholders = ",".join("?" for _ in plugin_ids)
    with get_db_context() as conn:
        conn.execute(
            f"UPDATE plugins SET enabled=?, {timestamp_column}=? "  # noqa: S608
            f"WHERE id IN ({placeholders}){only_builtin}",
            [value, now, *plugin_ids],
        )
        conn.commit()


def disable_plugin(plugin_id):
    """Mark a plugin as disabled and record the timestamp."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    update_plugin(plugin_id, enabled=0, disabled_at=now)


def delete_plugin(plugin_id):
    """Remove an external plugin row from the database.

    Only external (non-builtin) plugins may be deleted.  Returns ``True`` if a
    row was deleted, ``False`` otherwise.
    """
    with get_db_context() as conn:
        cur = conn.execute(
            "DELETE FROM plugins WHERE id = ? AND builtin = 0", (plugin_id,)
        )
        conn.commit()
        return cur.rowcount > 0


def remove_plugin(plugin_id):
    """Remove any plugin row from the database, regardless of its ``builtin`` flag.

    Used when uninstalling a plugin (including built-in ones) through the admin
    UI.  Returns ``True`` if a row was deleted, ``False`` if no row was found.
    """
    with get_db_context() as conn:
        cur = conn.execute("DELETE FROM plugins WHERE id = ?", (plugin_id,))
        conn.commit()
        return cur.rowcount > 0


@retry_on_busy
def is_plugin_enabled(plugin_id):
    """Return ``True`` if the plugin exists and is enabled."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT enabled FROM plugins WHERE id = ?", (plugin_id,)
        ).fetchone()
        return bool(row and row["enabled"])


def seed_builtin_plugins(plugin_manifests):
    """Ensure every built-in plugin has a row in the ``plugins`` table.

    *plugin_manifests* is a list of dicts, each with at least ``id``, ``name``,
    ``version``.  Existing rows are left untouched (the admin's enable/disable
    choice is preserved).

    Manifests with ``"experimental": true`` are skipped: experimental plugins
    must be installed manually by an administrator via the .bwplugin upload
    mechanism rather than being auto-discovered from the builtin directory.

    On a fresh install, a curated set of plugins providing core out-of-the-box
    functionality is enabled by default: ``attachments``, ``audit``,
    ``canvas``, ``chat``, ``drafts``, ``kanban``,
    ``page_history``, ``tts`` and ``user_data_export``.  All other built-in
    plugins are
    still pre-installed (rows are created in the ``plugins`` table) but start
    disabled so admins can opt in explicitly.
    """
    # A single worker prepares the registry: two starting together would
    # otherwise run the legacy rename and the seed loop against each other.
    with boot_lock(config.DATABASE_PATH, what="Plugin seeding"):
        _DEFAULT_ENABLED = {
            "attachments",
            "audit",
            "canvas",
            "chat",
            "drafts",
            "kanban",
            "page_history",
            "tts",
            "user_data_export",
        }
        _METADATA_REFRESH_IDS = {"chat"}
        # One-shot migrations for renamed builtin plugin ids.  ``page_reservations``
        # was merged into the new ``page_governance`` bundle (reservations +
        # protection + contribution approval), so any existing row carrying the
        # legacy id is renamed in place: preserving the admin's enabled flag:
        # before the regular seed loop runs.  Without this the old row would linger
        # alongside the new one and the admin's choice would silently be reset.
        with get_db_context() as conn:
            legacy = conn.execute(
                "SELECT enabled FROM plugins WHERE id = ?", ("page_reservations",)
            ).fetchone()
            if legacy is not None:
                existing_new = conn.execute(
                    "SELECT 1 FROM plugins WHERE id = ?", ("page_governance",)
                ).fetchone()
                if existing_new is None:
                    conn.execute(
                        "UPDATE plugins SET id = ?, name = ?, description = ? "
                        "WHERE id = ?",
                        (
                            "page_governance",
                            "Page Governance",
                            "Editorial governance bundle: page reservations, page "
                            "protection, and contribution approval.",
                            "page_reservations",
                        ),
                    )
                else:
                    # Both rows somehow exist: keep the new id but preserve any
                    # enabled flag carried over from the legacy row.
                    if legacy["enabled"]:
                        conn.execute(
                            "UPDATE plugins SET enabled = 1 WHERE id = ?",
                            ("page_governance",),
                        )
                    conn.execute(
                        "DELETE FROM plugins WHERE id = ?",
                        ("page_reservations",),
                    )
                conn.commit()

        for manifest in plugin_manifests:
            if manifest.get("experimental"):
                continue
            plugin_id = manifest["id"]
            inserted = register_plugin(
                plugin_id,
                name=manifest.get("name", plugin_id),
                version=manifest.get("version", "0.0.0"),
                author=manifest.get("author", "BananaWiki"),
                description=manifest.get("description", ""),
                builtin=True,
                enabled=plugin_id in _DEFAULT_ENABLED,
                update_existing=False,
            )
            if not inserted and plugin_id in _METADATA_REFRESH_IDS:
                update_plugin(
                    plugin_id,
                    name=manifest.get("name", plugin_id),
                    version=manifest.get("version", "0.0.0"),
                    author=manifest.get("author", "BananaWiki"),
                    description=manifest.get("description", ""),
                )
