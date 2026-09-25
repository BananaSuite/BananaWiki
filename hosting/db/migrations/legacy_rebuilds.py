"""Legacy instance-table rebuilds preserve dependent platform records."""

from sqlite_migrations import schema_transaction


def _instances_table_has_legacy_subdomain_unique(conn):
    """Return ``True`` when ``instances`` still has UNIQUE on ``subdomain`` alone.

    The legacy column declaration ``subdomain TEXT NOT NULL UNIQUE``
    creates a one-column auto-index whose ``PRAGMA index_info`` lists
    only ``subdomain``.  After the composite-unique migration the index
    is replaced with one covering ``(subdomain, domain_mode)`` and this
    function returns ``False`` so the migration is not re-run.

    Returns ``False`` if the ``instances`` table does not exist (fresh
    database: the ``CREATE TABLE`` above already used the right
    constraint).
    """
    has_instances = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instances'"
    ).fetchone()
    if not has_instances:
        return False

    indexes = conn.execute("PRAGMA index_list(instances)").fetchall()
    for idx in indexes:
        # PRAGMA index_list columns: seq, name, unique, origin, partial.
        name = idx[1]
        is_unique = bool(idx[2])
        if not is_unique:
            continue
        cols = conn.execute(f"PRAGMA index_info({name})").fetchall()
        # PRAGMA index_info columns: seqno, cid, column_name.
        if len(cols) == 1 and cols[0][2].lower() == "subdomain":
            return True
    return False


def _instances_table_has_not_null_expires_at(conn):
    """Return ``True`` when ``instances.expires_at`` is declared NOT NULL.

    Apex instances are intentionally created with ``expires_at = NULL``
    so a legacy ``NOT NULL`` constraint must be dropped before they can
    be inserted.  SQLite has no ``ALTER TABLE DROP NOT NULL`` so we
    detect the legacy constraint via ``PRAGMA table_info`` and rebuild
    the table.  Returns ``False`` when the table does not exist (fresh
    database: the ``CREATE TABLE`` above already uses the relaxed
    column definition).
    """
    has_instances = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instances'"
    ).fetchone()
    if not has_instances:
        return False
    for row in conn.execute("PRAGMA table_info(instances)").fetchall():
        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk.
        if row[1] == "expires_at":
            return bool(row[3])
    return False


def _rebuild_instances_with_composite_unique(conn):
    """Drop ``UNIQUE (subdomain)`` and re-create the table with composite uniqueness.

    SQLite has no ``ALTER TABLE DROP CONSTRAINT``, so the only way to
    remove a column-level UNIQUE is to copy the data into a fresh table
    that omits it and rename the new table back into place.  We preserve
    every existing column (including ones added by earlier migrations
    reading ``PRAGMA table_info`` and copying the intersection.

    Foreign-key enforcement is left to the caller. The wider migration
    runs while FK enforcement is already in the off-then-on bracket that
    a SQLite table-rebuild requires.  No other table in this schema
    references ``instances``, so the rebuild is safe regardless.
    """
    inst_cols_rows = conn.execute("PRAGMA table_info(instances)").fetchall()
    # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk.
    legacy_col_names = [row[1] for row in inst_cols_rows]
    if not legacy_col_names:
        return

    # Build the new table with every column that ``CREATE TABLE`` above
    # produces, plus all migration-added columns.  Listed in a fixed
    # order so the copy ``INSERT`` is deterministic regardless of the
    # legacy column ordering.
    # NOTE: ``expires_at`` is intentionally nullable now.  Apex /
    # vanity-domain instances (``domain_mode == 'apex'``) are
    # provisioned with ``expires_at = NULL`` to signal that they do
    # not auto-expire after the standard 14-day trial.  Older
    # databases declared this column ``NOT NULL``: the rebuild here
    # is what relaxes that.
    new_columns_sql = """
        id          TEXT PRIMARY KEY,
        account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        subdomain   TEXT NOT NULL COLLATE NOCASE,
        status      TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'stopped', 'suspended', 'terminated')),
        port        INTEGER,
        admin_username TEXT,
        admin_password_plain TEXT,
        storage_limit_mb INTEGER,
        upload_max_size_mb INTEGER,
        upload_blocked_extensions TEXT,
        created_at  TEXT NOT NULL,
        expires_at  TEXT,
        suspended_at TEXT,
        suspended_accumulated_seconds INTEGER NOT NULL DEFAULT 0,
        suspended_until TEXT,
        suspend_reason TEXT NOT NULL DEFAULT '',
        suspend_reason_visible INTEGER NOT NULL DEFAULT 0,
        suspend_time_visible INTEGER NOT NULL DEFAULT 0,
        stopped_at  TEXT,
        terminated_at TEXT,
        data_retained_until TEXT,
        terminated_reason TEXT,
        domain_mode TEXT NOT NULL DEFAULT 'hosting',
        declared_use_case TEXT NOT NULL DEFAULT '',
        tos_compliance_declared_at TEXT,
        grace_period_suspended INTEGER NOT NULL DEFAULT 0,
        custom_credentials INTEGER NOT NULL DEFAULT 0,
        oauth_client_id TEXT,
        oauth_client_secret_hash TEXT,
        easy_wiki INTEGER NOT NULL DEFAULT 0,
        public_wiki_allowed INTEGER NOT NULL DEFAULT 0,
        page_builder_allowed INTEGER NOT NULL DEFAULT 0,
        custom_domain_allowed INTEGER NOT NULL DEFAULT 0,
        UNIQUE (subdomain, domain_mode) ON CONFLICT ABORT
    """

    new_column_names = [
        "id", "account_id", "subdomain", "status", "port",
        "admin_username", "admin_password_plain",
        "storage_limit_mb", "upload_max_size_mb", "upload_blocked_extensions",
        "created_at", "expires_at",
        "suspended_at", "suspended_accumulated_seconds",
        "suspended_until", "suspend_reason", "suspend_reason_visible", "suspend_time_visible",
        "stopped_at", "terminated_at",
        "data_retained_until", "terminated_reason", "domain_mode",
        "declared_use_case", "tos_compliance_declared_at",
        "grace_period_suspended", "custom_credentials",
        "oauth_client_id", "oauth_client_secret_hash", "easy_wiki",
        "public_wiki_allowed", "page_builder_allowed", "custom_domain_allowed",
    ]
    # Only copy columns that exist in both the legacy and the new
    # schema.  This is identical to ``new_column_names`` in practice but
    # guards against unexpected legacy schemas.
    legacy_set = set(legacy_col_names)
    shared = [c for c in new_column_names if c in legacy_set]
    shared_sql = ", ".join(shared)

    with schema_transaction(conn):
        cur = conn.cursor()
        cur.execute(f"CREATE TABLE instances_new ({new_columns_sql})")
        cur.execute(
            f"INSERT INTO instances_new ({shared_sql}) SELECT {shared_sql} FROM instances"
        )
        cur.execute("DROP TABLE instances")
        cur.execute("ALTER TABLE instances_new RENAME TO instances")
