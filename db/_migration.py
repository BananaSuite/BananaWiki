"""Site data export and import."""

from datetime import datetime, timezone
import base64
import re
import sqlite3

from ._connection import SYSTEM_USER_ID, get_db_context


_MIGRATION_VERSION = 1
_VALID_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Tables exported in dependency order (parents before children).
_EXPORT_TABLES = [
    "users",
    "invite_codes",
    "invite_code_usage",
    "site_settings",
    "categories",
    "pages",
    "page_history",
    "drafts",
    "page_builder_drafts",
    "login_attempts",
    "announcements",
    "announcement_audience_users",
    "username_history",
    "editor_category_access",
    "editor_allowed_categories",
    "file_blobs",
    "page_attachments",
    "user_profiles",
    "profile_group_badges",
    "chats",
    "chat_messages",
    "chat_attachments",
    "group_chats",
    "group_members",
    "group_messages",
    "group_attachments",
    "role_history",
    "user_custom_tags",
    "user_permissions",
    "user_category_access",
    "user_allowed_categories",
    "badge_types",
    "user_badges",
    "badge_notifications",
    "page_reservations",
    "user_page_cooldowns",
    "reservation_quota_requests",
    "plugins",
    "api_tokens",
    "userbot_api_tokens",
    "impersonation_logs",
    "kanban_boards",
    "kanban_columns",
    "kanban_tickets",
    "kanban_board_shares",
    "kanban_ticket_attachments",
    "kanban_ticket_assignees",
    "kanban_ticket_history",
    "kanban_ticket_comments",
    "kanban_activity_log",
    "kanban_board_history",
    "kanban_events",
    "kanban_user_board_order",
    "canvas__layouts",
    "canvas__permissions",
    "canvas__history",
    "canvas__events",
    "canvas_user_layout_order",
    "assessments",
    "assessment_questions",
    "assessment_attempts",
    "assessment_answers",
    "temp_pages",
    "temp_page_index_state",
    "temp_users",
    "temp_roles",
    "custom_pages",
    "custom_page_files",
    "custom_roles",
    "custom_role_permissions",
    "custom_role_categories",
    "editing_sessions",
    "suspension_audit",
    "tts_generations",
    "analytics_daily",
    "account_merge_requests",
    "account_merge_logs",
    "pending_contributions",
    "contribution_quota_requests",
    "rate_limit_hits",
    "cleanup_lease",
    "api_service__tokens",
    "api_service__audit_log",
]
# Tables that are intentionally NOT exported because they hold only
# ephemeral / regenerable / operational state (rate-limit buckets, leader
# election leases, TTS audio cache).  Listing them here makes the policy
# explicit and prevents future contributors from accidentally exporting
# large transient blobs to site_export.json.
_EXPORT_EXCLUDED_TABLES = {
    "licenses",              # retired registration data; do not migrate it
    "rate_limit_hits",        # in-memory-style bucket counters, per-window
    "cleanup_lease",          # leader-election lease for periodic cleanup
    "tts_generations",        # regenerable TTS audio cache (can be huge)
    "user_sessions",          # live authentication state; never migrate tokens
    # Tables left behind by plugins that no longer ship. Nothing reads them, and
    # several hold credentials or personal data: users' own model API keys,
    # OAuth client secrets and bearer tokens, linked sign-in accounts, and the
    # IP addresses of people who sent feedback. They stay in the old database
    # and are not carried into a new one.
    "banana_ai__conversations", "banana_ai__credentials", "banana_ai__messages",
    "bw_oauth_provider__clients", "bw_oauth_provider__codes", "bw_oauth_provider__tokens",
    "oauth_login__connections",
    "git_override__commits",
    "meetings__rooms", "meetings__participants", "meetings__signals", "meetings__chat_messages",
    "feedback_reports", "feedback_banned_users",
    "feedback__reports", "feedback__bans", "feedback__telemetry_logs",
    "beta_testers", "beta_tester_invites",
}

# Rows in these tables act on an account instead of on content, and they reach
# an existing account without conflicting with anything already stored: an
# expired temp_roles row sets the account's role to its original_role (which
# the file chooses), a temp_users row gets the account deleted when it
# expires, a token row lets whoever holds the token sign in as the account,
# and a merge request can fold the account into another one. The values are
# the columns that hold the account id.
_ACCOUNT_CONTROL_TABLES = {
    "temp_roles": ("user_id",),
    "temp_users": ("user_id",),
    "api_tokens": ("user_id",),
    "userbot_api_tokens": ("user_id",),
    "api_service__tokens": ("user_id",),
    "account_merge_requests": ("source_user_id", "target_user_id"),
}


def _get_migration_tables(conn):
    """Return all user tables in a stable migration order."""
    existing_tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if _VALID_TABLE_NAME_RE.fullmatch(row["name"])
    }
    existing_tables -= _EXPORT_EXCLUDED_TABLES
    ordered_tables = [table for table in _EXPORT_TABLES if table in existing_tables]
    extra_tables = sorted(existing_tables - set(ordered_tables))
    return ordered_tables + extra_tables


def _row_to_dict(row):
    """Convert a DB row to a JSON-serialisable dict.

    Any bytes/bytearray/memoryview values are wrapped as
    ``{"__b64__": "<base64>"}`` so they survive JSON round-trips.
    """
    d = {}
    for k in row.keys():
        v = row[k]
        if isinstance(v, (bytes, bytearray, memoryview)):
            d[k] = {"__b64__": base64.b64encode(bytes(v)).decode("ascii")}
        else:
            d[k] = v
    return d


def _dict_from_import(d):
    """Reverse _row_to_dict(): decode ``{"__b64__": ...}`` values back to bytes."""
    result = {}
    for k, v in d.items():
        if isinstance(v, dict) and "__b64__" in v and len(v) == 1:
            result[k] = base64.b64decode(v["__b64__"])
        else:
            result[k] = v
    return result


def _attribute_imported_pages_to_system(data):
    """Return a copy of *data* with imported page authors assigned to the system."""
    if not isinstance(data, dict):
        return data
    system_id = str(SYSTEM_USER_ID)
    normalized = dict(data)
    for table, column in (("pages", "last_edited_by"), ("page_history", "edited_by")):
        rows = data.get(table)
        if not isinstance(rows, list):
            continue
        normalized_rows = []
        for row in rows:
            if isinstance(row, dict):
                row = dict(row)
                if column in row:
                    row[column] = system_id
            normalized_rows.append(row)
        normalized[table] = normalized_rows
    return normalized


def _is_home_flag(value):
    """Return True when an imported is_home value represents a home page."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _dedupe_imported_home_pages(data):
    """Keep only the first imported page marked as home."""
    if not isinstance(data, dict):
        return data
    rows = data.get("pages")
    if not isinstance(rows, list):
        return data
    normalized = dict(data)
    normalized_rows = []
    home_seen = False
    for row in rows:
        if isinstance(row, dict) and _is_home_flag(row.get("is_home")):
            row = dict(row)
            if home_seen:
                row["is_home"] = 0
            else:
                home_seen = True
        normalized_rows.append(row)
    normalized["pages"] = normalized_rows
    return normalized


def _import_includes_home_page(data):
    """Return True if the imported page rows contain a home page."""
    rows = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return False
    return any(isinstance(row, dict) and _is_home_flag(row.get("is_home")) for row in rows)


def _normalize_home_page_invariants(conn):
    """Repair states that are incompatible with the designated home page."""
    home_rows = conn.execute("SELECT id FROM pages WHERE is_home=1 ORDER BY id").fetchall()
    if len(home_rows) > 1:
        keep_id = home_rows[0]["id"]
        conn.execute("UPDATE pages SET is_home=0 WHERE is_home=1 AND id != ?", (keep_id,))
        home_rows = conn.execute("SELECT id FROM pages WHERE is_home=1 ORDER BY id").fetchall()
    if not home_rows:
        fallback = conn.execute("SELECT id FROM pages ORDER BY id LIMIT 1").fetchone()
        if not fallback:
            conn.execute(
                "INSERT INTO pages (title, slug, content, is_home) VALUES (?, ?, ?, 1)",
                ("Home", "home", "# Welcome to your Wiki\n\nEdit this page to get started."),
            )
        else:
            conn.execute("UPDATE pages SET is_home=1 WHERE id=?", (fallback["id"],))
    conn.execute(
        "UPDATE pages SET is_deindexed=0, pending_deletion=0, "
        "pending_deletion_by=NULL, pending_deletion_at=NULL, "
        "protected_by=NULL, protected_at=NULL, "
        "protection_unlock_requested_at=NULL, protection_unlock_requested_by=NULL "
        "WHERE is_home=1"
    )
    conn.execute("DELETE FROM temp_pages WHERE page_id IN (SELECT id FROM pages WHERE is_home=1)")
    conn.execute(
        "DELETE FROM temp_page_index_state WHERE page_id IN (SELECT id FROM pages WHERE is_home=1)"
    )
    conn.execute("DELETE FROM page_reservations WHERE page_id IN (SELECT id FROM pages WHERE is_home=1)")
    conn.execute("DELETE FROM user_page_cooldowns WHERE page_id IN (SELECT id FROM pages WHERE is_home=1)")


def _filter_intentional_system_fk_errors(conn, fk_errors):
    """Drop foreign-key-check rows caused only by the system author sentinel."""
    system_id = str(SYSTEM_USER_ID)
    system_columns = {
        "pages": "last_edited_by",
        "page_history": "edited_by",
    }
    filtered = []
    fk_column_cache = {}
    for error in fk_errors:
        table = error["table"] if "table" in error.keys() else error[0]
        rowid = error["rowid"] if "rowid" in error.keys() else error[1]
        parent = error["parent"] if "parent" in error.keys() else error[2]
        fkid = error["fkid"] if "fkid" in error.keys() else error[3]
        column = system_columns.get(table)
        if not column or parent != "users":
            filtered.append(error)
            continue
        cache_key = (table, fkid)
        if cache_key not in fk_column_cache:
            fk_row = conn.execute(
                f"PRAGMA foreign_key_list({table})"  # noqa: S608
            ).fetchall()
            fk_column_cache[cache_key] = next(
                (r["from"] for r in fk_row if r["id"] == fkid),
                None,
            )
        if fk_column_cache[cache_key] != column:
            filtered.append(error)
            continue
        row = conn.execute(
            f"SELECT {column} FROM {table} WHERE rowid=?",  # noqa: S608
            (rowid,),
        ).fetchone()
        if not row or str(row[column]) != system_id:
            filtered.append(error)
    return filtered


def _export_from_conn(conn):
    """Internal: dump every migration-eligible table from *conn* to JSON-safe dicts."""
    data = {
        "_meta": {
            "version": _MIGRATION_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    for table in _get_migration_tables(conn):
        # Safe: _get_migration_tables() filters sqlite_master names through
        # _VALID_TABLE_NAME_RE before returning them.
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        data[table] = [_row_to_dict(r) for r in rows]
    return data


def export_site_data(*, db_path=None):
    """Return a dict containing all site data suitable for JSON serialisation.

    The dict has the shape::

        {
            "_meta": {"version": 1, "exported_at": "<iso8601>"},
            "users": [...],
            "invite_codes": [...],
            ...
        }

    When *db_path* is ``None`` (the default), the active wiki's database
    is used.  Pass a path to read a specific SQLite file on disk. The
    hosting platform uses this to dump a per-instance ``bananawiki.db``
    snapshot without disturbing the live process.
    """
    if db_path is None:
        with get_db_context() as conn:
            return _export_from_conn(conn)

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return _export_from_conn(conn)
    finally:
        conn.close()


def _validate_delete_all_import(data):
    """Verify *data* is safe for ``delete_all`` mode.

    Raises ``ValueError`` if:
    * There is no ``users`` entry containing at least one ``admin`` or
      ``owner`` role.
    * There is no ``site_settings`` entry with ``setup_done`` equal to ``1``.

    Without these checks, a skeleton or empty export would wipe the entire
    database and leave the wiki in an unrecoverable state.
    """
    users = data.get("users", [])
    has_admin = any(
        u.get("role") in ("admin", "owner") for u in users
    )
    if not has_admin:
        raise ValueError(
            "Import rejected: the import data does not contain any admin "
            "or owner user. A delete_all import without an admin "
            "account would leave the wiki unrecoverable."
        )

    settings_rows = data.get("site_settings", [])
    has_setup_done = any(
        s.get("setup_done") == 1 for s in settings_rows
    )
    if not has_setup_done:
        raise ValueError(
            "Import rejected: the import data does not contain a "
            "site_settings row with setup_done=1. A delete_all import "
            "without this would leave the wiki unrecoverable."
        )
    if not _import_includes_home_page(data):
        raise ValueError(
            "Import rejected: the import data does not contain a designated "
            "home page. A delete_all import without this would leave the "
            "wiki without a landing page."
        )


def _as_stored_text(conn, value):
    """Return *value* as SQLite would store it in a TEXT id column.

    The account ids in ``users`` and in the tables that point at it are TEXT,
    and SQLite converts other values on the way in: a JSON ``true`` is bound
    as 1 and stored as ``'1'``, the id of the first account (usually the
    owner) on installations upgraded from integer ids. Python's ``str(True)``
    is ``'True'``, so comparing ``str()`` output would let such a row through.
    Numbers go through SQLite's own conversion for the same reason. A blob
    stays a blob in the column and never equals a TEXT id, but it is compared
    by its text here anyway: that can only leave out more rows, never fewer.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, (int, float)):
        return conn.execute("SELECT CAST(? AS TEXT)", (value,)).fetchone()[0]
    return str(value)


def _protect_existing_accounts(conn, data):
    """Leave out imported rows that would change accounts already on this wiki.

    Used by the ``keep`` mode, which merges a file into a live wiki. That mode
    already keeps the local copy of any conflicting row, so the file's copy of
    an existing account (same id, or same username under the column's NOCASE
    rule) is never written. The tables in ``_ACCOUNT_CONTROL_TABLES`` reach an
    account without a conflict, though, so a merge could still raise a role,
    demote or delete the owner later, or hand out a way to sign in as a
    superuser. Their rows are left out whenever they point at an existing
    account, or at a file account that was left out for clashing with one.

    Ids are compared after :func:`_as_stored_text`, on the values the import
    will actually write (``{"__b64__": ...}`` already decoded).

    Returns ``(data, skipped)``: a filtered copy of *data* and a dict mapping
    each table name to the number of rows left out of it.
    """
    skipped = {}
    protected_ids = {
        _as_stored_text(conn, row["id"])
        for row in conn.execute("SELECT id FROM users").fetchall()
    }
    protected_ids.discard(None)
    filtered = dict(data)

    users = data.get("users")
    if isinstance(users, list):
        kept_users = []
        for row in users:
            if isinstance(row, dict):
                decoded = _dict_from_import(row)
                row_id = _as_stored_text(conn, decoded.get("id"))
                username = _as_stored_text(conn, decoded.get("username"))
                clash = conn.execute(
                    "SELECT 1 FROM users WHERE id = ? OR username = ? LIMIT 1",
                    (row_id, username),
                ).fetchone()
                if clash:
                    if row_id is not None:
                        protected_ids.add(row_id)
                    skipped["users"] = skipped.get("users", 0) + 1
                    continue
            kept_users.append(row)
        filtered["users"] = kept_users

    for table, columns in _ACCOUNT_CONTROL_TABLES.items():
        rows = data.get(table)
        if not isinstance(rows, list):
            continue
        kept_rows = []
        for row in rows:
            if isinstance(row, dict):
                decoded = _dict_from_import(row)
                if any(
                    _as_stored_text(conn, decoded.get(column)) in protected_ids
                    for column in columns
                ):
                    skipped[table] = skipped.get(table, 0) + 1
                    continue
            kept_rows.append(row)
        filtered[table] = kept_rows

    return filtered, skipped


def _import_into_conn(conn, data, mode):
    """Internal: apply *data* in *mode* using an already-open SQLite connection.

    Returns the ``skipped`` dict from :func:`_apply_import`.
    """
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        skipped = _apply_import(conn, data, mode)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        fk_errors = _filter_intentional_system_fk_errors(conn, fk_errors)
        if fk_errors:
            import logging
            logging.getLogger("bananawiki").warning(
                "Foreign key violations detected after import: %d error(s)",
                len(fk_errors),
            )
    return skipped


def _apply_import(conn, data, mode):
    """Apply the import SQL.  See ``_import_into_conn`` for the txn wrapper.

    Returns a dict mapping table names to the number of imported rows left
    out to protect existing accounts. Only the ``keep`` mode leaves rows out;
    ``delete_all`` and ``override`` let the file replace any account.
    """
    tables = _get_migration_tables(conn)
    skipped = {}

    # A merge keeps every account that is already here as it is: no role or
    # superuser change, no scheduled demotion or deletion, no new sign-in
    # token. The other two modes replace accounts by design, which is how a
    # fresh wiki's setup account gives way to the accounts of a moved wiki.
    if mode == "keep":
        data, skipped = _protect_existing_accounts(conn, data)

    # Authentication state is never portable. Clear it for every import mode
    # so a cookie issued before an account overwrite cannot authenticate as
    # the imported identity, and imported legacy tokens cannot revive access.
    conn.execute("DELETE FROM user_sessions")

    if mode == "delete_all":
        for table in reversed(tables):
            if table == "site_settings":
                continue
            conn.execute(f"DELETE FROM {table}")  # noqa: S608

    # Before importing pages, clear all is_home flags so that only the
    # imported home page (if any) retains the designation.  Without this
    # the override / keep modes could leave multiple rows with is_home=1.
    if (
        mode != "keep"
        and "pages" in tables
        and data.get("pages")
        and _import_includes_home_page(data)
    ):
        conn.execute("UPDATE pages SET is_home=0")
    keep_existing_home = (
        mode == "keep"
        and conn.execute("SELECT 1 FROM pages WHERE is_home=1 LIMIT 1").fetchone()
        is not None
    )

    insert_prefix = {
        "delete_all": "INSERT OR REPLACE",
        "override": "INSERT OR REPLACE",
        "keep": "INSERT OR IGNORE",
    }[mode]

    for table in tables:
        rows = data.get(table, [])
        if not rows:
            continue
        conn_cols = {
            r[1]
            for r in conn.execute(
                f"PRAGMA table_info({table})"  # noqa: S608
            ).fetchall()
        }
        import_cols = [c for c in rows[0].keys() if c in conn_cols]
        if not import_cols:
            continue
        col_str = ", ".join(import_cols)
        placeholders = ", ".join("?" for _ in import_cols)
        sql = (
            f"{insert_prefix} INTO {table} ({col_str}) "  # noqa: S608
            f"VALUES ({placeholders})"
        )
        if table == "site_settings" and mode == "delete_all":
            sql = (
                f"INSERT OR REPLACE INTO {table} ({col_str}) "  # noqa: S608
                f"VALUES ({placeholders})"
            )
        for row in rows:
            decoded_row = _dict_from_import(row)
            if (
                table == "pages"
                and keep_existing_home
                and _is_home_flag(decoded_row.get("is_home"))
            ):
                decoded_row["is_home"] = 0
            vals = [decoded_row.get(c) for c in import_cols]
            conn.execute(sql, vals)
    if "pages" in tables:
        _normalize_home_page_invariants(conn)
    conn.execute("UPDATE users SET session_token=NULL")
    return skipped


def import_site_data(data, mode, *, db_path=None):
    """Import site data from a previously exported dict.

    ``mode`` must be one of:

    * ``"delete_all"``: clear all existing data first, then insert everything
      from the export.
    * ``"override"``: keep existing data but replace any conflicting rows with
      the imported values (``INSERT OR REPLACE``).
    * ``"keep"``: keep existing data; silently skip any conflicting rows
      (``INSERT OR IGNORE``). Accounts that already exist are never changed,
      see :func:`_protect_existing_accounts`.

    ``delete_all`` and ``override`` can replace every account, the owner's
    included, and all three modes can add accounts with any role. Callers
    must treat an import as handing over complete control of the wiki.

    When *db_path* is ``None`` (the default), the active wiki's database
    is used.  Pass a path to import into a specific SQLite file on disk.
    The hosting platform uses this to reconstruct a per-instance DB from
    a ``site_export.json`` dump during import.

    Returns a dict mapping table names to the number of imported rows the
    ``keep`` mode left out to protect existing accounts (empty for the other
    modes).

    Raises ``ValueError`` for an unrecognised mode or an incompatible export
    version.
    """
    if mode not in ("delete_all", "override", "keep"):
        raise ValueError(f"Unknown import mode: {mode!r}")

    meta = data.get("_meta", {})
    version = meta.get("version", 1)
    if version != _MIGRATION_VERSION:
        raise ValueError(
            f"Incompatible export version {version!r} "
            f"(expected {_MIGRATION_VERSION})"
        )

    if mode == "delete_all":
        _validate_delete_all_import(data)

    data = _attribute_imported_pages_to_system(data)
    data = _dedupe_imported_home_pages(data)

    if db_path is not None:
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            return _import_into_conn(conn, data, mode)
        finally:
            conn.close()

    with get_db_context() as conn:
        return _import_into_conn(conn, data, mode)
