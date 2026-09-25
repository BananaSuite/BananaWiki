"""Database helpers for plugins.

Plugins use two helpers instead of importing ``db``:

* ``db_query(sql, params)``: one read-only statement
* ``db_execute(sql, params)``: one statement that may write, but never to a
  core table

These are guard rails for honest plugins, not a security boundary.  A
plugin runs inside the wiki process with the wiki's own privileges: it can
import ``db`` or ``sqlite3`` and change anything.  What the helpers promise
is narrower: a mistake in a well-meant plugin cannot damage core data
through them.

Both rules are enforced by SQLite's authorizer, which sees each statement
as SQLite parsed it.  Schema-qualified names (``main.users``), comments,
``OR REPLACE`` forms and common table expressions therefore get the same
answer as the plain spelling.
"""

import functools
import sqlite3

from bananawiki_sdk._exceptions import PluginError

# Tables that belong to the BananaWiki core.  Plugin code may read them but
# must never write to them.  The live list also takes in every table the
# site export knows about (see ``_core_tables``), so a table added to the
# schema and to the export is protected without editing this set.
_CORE_TABLES = frozenset({
    "account_merge_logs", "account_merge_requests", "analytics_daily",
    "announcement_audience_users", "announcements",
    "api_service__audit_log", "api_service__tokens", "api_tokens",
    "assessment_answers", "assessment_attempts", "assessment_questions",
    "assessments",
    "badge_notifications", "badge_types", "beta_tester_invites", "beta_testers",
    "canvas__events", "canvas__history", "canvas__layouts",
    "canvas__permissions", "canvas_user_layout_order",
    "categories", "chat_attachments", "chat_messages", "chats",
    "cleanup_lease", "contribution_quota_requests",
    "custom_page_files", "custom_pages", "custom_role_categories",
    "custom_role_permissions", "custom_roles",
    "drafts", "editing_sessions", "editor_allowed_categories",
    "editor_category_access",
    "federation_copies", "federation_identity", "federation_nonces",
    "federation_page_ids", "federation_peers", "federation_shares",
    "file_blobs",
    "group_attachments", "group_chats", "group_members", "group_messages",
    "impersonation_logs", "invite_code_usage", "invite_codes",
    "kanban_activity_log", "kanban_board_history", "kanban_board_shares",
    "kanban_boards", "kanban_columns", "kanban_events",
    "kanban_ticket_assignees", "kanban_ticket_attachments",
    "kanban_ticket_comments", "kanban_ticket_history", "kanban_tickets",
    "kanban_user_board_order",
    "login_attempts", "page_attachments", "page_builder_drafts",
    "page_history", "page_reservations", "pages", "pending_contributions",
    "plugins", "profile_group_badges", "rate_limit_hits",
    "reservation_quota_requests", "role_history", "site_settings",
    "suspension_audit", "temp_page_index_state", "temp_pages", "temp_roles",
    "temp_users", "tts_generations",
    "user_allowed_categories", "user_badges", "user_category_access",
    "user_custom_tags", "user_page_cooldowns", "user_permissions",
    "user_profiles", "user_sessions", "userbot_api_tokens",
    "username_history", "users",
})

# SQLite's own bookkeeping tables.  Creating or dropping a plugin table
# writes to them as a side effect, so the write guard lets those through.
# SQLite itself refuses direct changes to the schema table.
_SQLITE_INTERNAL_TABLES = frozenset({
    "sqlite_master", "sqlite_schema", "sqlite_temp_master",
    "sqlite_temp_schema", "sqlite_sequence",
})

# PRAGMAs that only describe the schema.  Every other PRAGMA can change how
# the shared connection behaves (foreign_keys, writable_schema, ...) and is
# refused.
_READ_ONLY_PRAGMAS = frozenset({
    "table_info", "table_xinfo", "table_list", "index_list", "index_info",
    "index_xinfo", "foreign_key_list",
})


def _action(name):
    """Return the authorizer action code *name*, or None on old SQLite builds."""
    return getattr(sqlite3, name, None)


# Actions that change the database, mapped to the callback argument that
# names the table being changed (0 for the first argument, 1 for the second).
_WRITE_ACTIONS = {
    code: position
    for code, position in (
        (_action("SQLITE_INSERT"), 0),
        (_action("SQLITE_UPDATE"), 0),
        (_action("SQLITE_DELETE"), 0),
        (_action("SQLITE_CREATE_TABLE"), 0),
        (_action("SQLITE_CREATE_TEMP_TABLE"), 0),
        (_action("SQLITE_DROP_TABLE"), 0),
        (_action("SQLITE_DROP_TEMP_TABLE"), 0),
        (_action("SQLITE_ALTER_TABLE"), 1),
        (_action("SQLITE_ANALYZE"), 0),
        (_action("SQLITE_CREATE_INDEX"), 1),
        (_action("SQLITE_CREATE_TEMP_INDEX"), 1),
        (_action("SQLITE_DROP_INDEX"), 1),
        (_action("SQLITE_DROP_TEMP_INDEX"), 1),
        (_action("SQLITE_CREATE_TRIGGER"), 1),
        (_action("SQLITE_CREATE_TEMP_TRIGGER"), 1),
        (_action("SQLITE_DROP_TRIGGER"), 1),
        (_action("SQLITE_DROP_TEMP_TRIGGER"), 1),
        (_action("SQLITE_CREATE_VIEW"), 0),
        (_action("SQLITE_CREATE_TEMP_VIEW"), 0),
        (_action("SQLITE_DROP_VIEW"), 0),
        (_action("SQLITE_DROP_TEMP_VIEW"), 0),
        (_action("SQLITE_CREATE_VTABLE"), 0),
        (_action("SQLITE_DROP_VTABLE"), 0),
    )
    if code is not None
}
_ALWAYS_DENIED = frozenset(
    code for code in (_action("SQLITE_ATTACH"), _action("SQLITE_DETACH"))
    if code is not None
)
_PRAGMA = _action("SQLITE_PRAGMA")


@functools.lru_cache(maxsize=1)
def _core_tables():
    """Return the lower-cased names of every core table."""
    names = set(_CORE_TABLES)
    try:
        from db import _migration
        names.update(_migration._EXPORT_TABLES)
        names.update(_migration._EXPORT_EXCLUDED_TABLES)
    except (ImportError, AttributeError):
        pass
    return frozenset(name.lower() for name in names)


def _check_single_statement(sql, params):
    """Reject multi-statement SQL and parameters of the wrong type."""
    # The old check (`if ";" in sql and not sql.strip().endswith(";")`) could
    # be bypassed by SQL like "SELECT 1; DROP TABLE x;" which ends with ";".
    # This stronger check counts semicolons and rejects anything with more
    # than one statement.
    stripped = sql.strip()
    if stripped.count(";") > 1 or (stripped.count(";") == 1 and not stripped.endswith(";")):
        raise PluginError(
            "Multi-statement SQL is not allowed. Use a single statement."
        )
    if params is not None and not isinstance(params, (list, tuple)):
        raise PluginError(
            "Query parameters must be a list or tuple for security."
        )


def _pragma_allowed(pragma_name):
    return (pragma_name or "").lower() in _READ_ONLY_PRAGMAS


def _read_only_authorizer(denied):
    """Build an authorizer that refuses every change to the database."""
    def authorize(action, arg1, arg2, _db_name, _trigger):
        if action in _ALWAYS_DENIED:
            denied.append(("statement", action))
            return sqlite3.SQLITE_DENY
        if action == _PRAGMA and not _pragma_allowed(arg1):
            denied.append(("pragma", arg1))
            return sqlite3.SQLITE_DENY
        if action in _WRITE_ACTIONS:
            denied.append(("write", arg1))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return authorize


def _plugin_write_authorizer(denied):
    """Build an authorizer that refuses changes to core tables."""
    core = _core_tables()

    def authorize(action, arg1, arg2, db_name, _trigger):
        if action in _ALWAYS_DENIED:
            denied.append(("statement", action))
            return sqlite3.SQLITE_DENY
        if action == _PRAGMA and not _pragma_allowed(arg1):
            denied.append(("pragma", arg1))
            return sqlite3.SQLITE_DENY
        position = _WRITE_ACTIONS.get(action)
        if position is None:
            return sqlite3.SQLITE_OK
        table = (arg1, arg2)[position] or ""
        lowered = table.lower()
        if lowered in _SQLITE_INTERNAL_TABLES or db_name == "temp":
            return sqlite3.SQLITE_OK
        if lowered in core:
            denied.append(("core", table))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return authorize


def _denied_error(denied, *, read_only):
    """Turn the first refused action into a plugin-facing error."""
    kind, detail = denied[0]
    if read_only and kind == "write":
        return PluginError(
            "db_query is read-only. Use db_execute for write operations "
            "on plugin-owned tables."
        )
    if kind == "core":
        return PluginError(
            f"db_execute cannot write to core table '{detail.lower()}'. "
            f"Plugin code may only write to plugin-owned tables."
        )
    if kind == "pragma":
        return PluginError(
            f"PRAGMA {detail} is not available to plugins. Only schema "
            f"introspection PRAGMAs such as table_info are allowed."
        )
    return PluginError("ATTACH and DETACH are not available to plugins.")


def db_query(sql, params=None):
    """Execute a **read-only** SQL query against the BananaWiki database.

    Returns a list of ``sqlite3.Row`` objects.

    Raises :class:`PluginError` if the statement would change the database
    or contains multiple statements.
    """
    _check_single_statement(sql, params)
    from db import get_db
    conn = get_db()
    denied = []
    try:
        conn.set_authorizer(_read_only_authorizer(denied))
        try:
            return conn.execute(sql, params or []).fetchall()
        except sqlite3.DatabaseError as exc:
            if denied:
                raise _denied_error(denied, read_only=True) from exc
            raise
    finally:
        conn.close()


def db_execute(sql, params=None):
    """Execute a **write** SQL statement against plugin-owned tables.

    The statement may not change a core table, attach another database or
    run a PRAGMA other than schema introspection.  It must be a single
    statement.

    Returns the ``cursor.lastrowid`` for INSERT statements.

    Raises :class:`PluginError` when any of those rules is broken.
    """
    _check_single_statement(sql, params)
    from db import get_db
    conn = get_db()
    denied = []
    try:
        conn.set_authorizer(_plugin_write_authorizer(denied))
        try:
            cur = conn.execute(sql, params or [])
        except sqlite3.DatabaseError as exc:
            if denied:
                raise _denied_error(denied, read_only=False) from exc
            raise
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()
