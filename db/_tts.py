"""Text-to-speech generation cache: DB layer for the built-in ``tts`` plugin.

A single ``tts_generations`` row tracks the latest MP3 cache for each page.
``page_id`` is UNIQUE: requesting a new generation supersedes the old row,
which is how page-update invalidation works.

State machine::

    [no row]
        |  request_tts_generation()
        v
    pending  ---->  processing  ---->  completed
        |              |
        +--------------+----> failed

Callers must use these helpers; routes never touch the table directly.
"""

from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy

# The DB-side language validator delegates to the engine-side catalogue so
# the two layers cannot drift.  Importing :mod:`helpers._tts` here is safe
# because that module has no Flask dependencies; it only imports stdlib
# plus this very ``db`` package (via ``import db``), which by the time
# ``_tts.py`` runs has already finished its lazy public-API exports.  We
# import the canonical set rather than the tuple so membership checks stay
# O(1).
from helpers._tts import TTS_SUPPORTED_LANGUAGE_SET as _VALID_LANGUAGES

_VALID_STATUSES = ("pending", "processing", "completed", "failed")


def _utc_now_iso():
    """Return the current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _coerce_positive_limit(value, default):
    """Return *value* as a positive integer, falling back to *default*."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _has_usable_tts_file(row):
    """Return True when *row* points at a completed non-empty audio file."""
    return bool(
        row
        and row["status"] == "completed"
        and row["filename"]
        and (row["file_size"] or 0) > 0
    )


@retry_on_busy
def get_tts_generation(page_id):
    """Return the ``tts_generations`` row for *page_id* or ``None``."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM tts_generations WHERE page_id = ?",
            (page_id,),
        ).fetchone()


@retry_on_busy
def get_tts_generation_by_id(generation_id):
    """Return a generation row by its primary key (or ``None``)."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM tts_generations WHERE id = ?",
            (generation_id,),
        ).fetchone()


def request_tts_generation(page_id, language, content_hash, requested_by):
    """Atomically reserve a generation slot for *page_id*.

    Returns a tuple ``(row, created)`` where:

    - ``row`` is the current ``tts_generations`` row for *page_id*.
    - ``created`` is ``True`` when this call inserted a new row, ``False``
      when an existing row already covered the request.

    Behaviour:

    - If a row exists with status ``pending`` / ``processing`` for the same
      content hash, ``created`` is ``False`` (let the existing worker finish).
    - If a row exists with status ``completed`` for the same content + same
      language, ``created`` is ``False`` (cache hit: caller can serve it).
    - In every other case (different language, different content hash, or
      ``failed`` status) the existing row is replaced with a fresh ``pending``
      row and ``created`` is ``True``.
    """
    if language not in _VALID_LANGUAGES:
        raise ValueError(f"unsupported language: {language!r}")

    now = _utc_now_iso()

    with get_db_context() as conn:
        existing = conn.execute(
            "SELECT * FROM tts_generations WHERE page_id = ?",
            (page_id,),
        ).fetchone()

        if existing:
            same_content = existing["content_hash"] == content_hash
            same_language = existing["language"] == language
            if same_content and same_language and existing["status"] in (
                "pending", "processing",
            ):
                return existing, False
            if same_content and same_language and existing["status"] == "completed":
                return existing, False
            # Otherwise the cache is stale (different content / language) or
            # the previous attempt failed: drop it so a fresh worker starts.
            conn.execute(
                "DELETE FROM tts_generations WHERE id = ?",
                (existing["id"],),
            )

        conn.execute(
            "INSERT INTO tts_generations "
            "(page_id, language, status, content_hash, requested_by, requested_at) "
            "VALUES (?, ?, 'pending', ?, ?, ?)",
            (page_id, language, content_hash, requested_by, now),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM tts_generations WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        return row, True


def request_limited_tts_generation(
    page_id,
    language,
    content_hash,
    requested_by,
    *,
    max_active_jobs,
    max_active_for_requester,
):
    """Atomically reserve a manual/on-demand TTS slot with active-job caps.

    This is the route-facing variant of :func:`request_tts_generation`.
    It uses ``BEGIN IMMEDIATE`` so concurrent web requests cannot all observe
    spare queue capacity and insert multiple pending rows at once.

    Returns ``(row, created, outcome)``:

    - ``created`` is ``True`` only for a freshly inserted pending row.
    - ``outcome`` is ``None`` on a newly accepted row, or a dict with
      ``reason`` and queue/cache metadata that the route can translate into an
      HTTP response.

    Unlike the auto/backfill helper, an existing pending/processing row for
    this page always wins: manual readers should wait for that work rather
    than superseding it from a second browser tab.
    """
    if language not in _VALID_LANGUAGES:
        raise ValueError(f"unsupported language: {language!r}")

    max_active_jobs = _coerce_positive_limit(max_active_jobs, 1)
    max_active_for_requester = _coerce_positive_limit(
        max_active_for_requester, 1,
    )
    now = _utc_now_iso()

    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM tts_generations WHERE page_id = ?",
                (page_id,),
            ).fetchone()

            if existing:
                if existing["status"] in ("pending", "processing"):
                    conn.commit()
                    return existing, False, {
                        "reason": "already_in_progress",
                    }
                if _has_usable_tts_file(existing):
                    conn.commit()
                    return existing, False, {
                        "reason": "already_generated",
                    }

            active_total = conn.execute(
                "SELECT COUNT(*) AS n FROM tts_generations "
                "WHERE status IN ('pending', 'processing')"
            ).fetchone()
            active_total = int(active_total["n"] if active_total else 0)
            if active_total >= max_active_jobs:
                conn.commit()
                return existing, False, {
                    "reason": "tts_queue_full",
                    "active_jobs": active_total,
                    "max_active_jobs": max_active_jobs,
                }

            if requested_by is not None:
                active_for_user = conn.execute(
                    "SELECT COUNT(*) AS n FROM tts_generations "
                    "WHERE status IN ('pending', 'processing') "
                    "  AND requested_by = ?",
                    (requested_by,),
                ).fetchone()
                active_for_user = int(
                    active_for_user["n"] if active_for_user else 0
                )
                if active_for_user >= max_active_for_requester:
                    conn.commit()
                    return existing, False, {
                        "reason": "tts_user_queue_full",
                        "active_jobs": active_for_user,
                        "max_active_jobs": max_active_for_requester,
                    }

            if existing:
                conn.execute(
                    "DELETE FROM tts_generations WHERE id = ?",
                    (existing["id"],),
                )

            conn.execute(
                "INSERT INTO tts_generations "
                "(page_id, language, status, content_hash, requested_by, requested_at) "
                "VALUES (?, ?, 'pending', ?, ?, ?)",
                (page_id, language, content_hash, requested_by, now),
            )
            row = conn.execute(
                "SELECT * FROM tts_generations WHERE page_id = ?",
                (page_id,),
            ).fetchone()
            conn.commit()
            return row, True, None
        except Exception:
            conn.execute("ROLLBACK")
            raise


def mark_tts_processing(generation_id):
    """Mark *generation_id* as ``processing`` and stamp ``started_at``.

    Returns ``True`` when the row transitioned from ``pending`` -> ``processing``.
    Returns ``False`` if the row no longer exists or is no longer ``pending``
    (e.g. it was cancelled by a page update before the worker started).
    """
    now = _utc_now_iso()
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE tts_generations "
            "SET status = 'processing', started_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, generation_id),
        )
        conn.commit()
        return cur.rowcount > 0


def mark_tts_completed(generation_id, filename, file_size):
    """Mark *generation_id* as ``completed`` with the saved file metadata.

    Returns ``True`` when the row transitioned from ``processing`` ->
    ``completed``.  Returns ``False`` when the row was deleted/cancelled
    while the worker was running, in which case the caller should delete
    the on-disk MP3 it just wrote.
    """
    now = _utc_now_iso()
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE tts_generations "
            "SET status = 'completed', filename = ?, file_size = ?, "
            "    completed_at = ?, error_message = NULL "
            "WHERE id = ? AND status = 'processing'",
            (filename, file_size, now, generation_id),
        )
        conn.commit()
        return cur.rowcount > 0


def mark_tts_failed(generation_id, error_message):
    """Mark *generation_id* as ``failed`` with an *error_message*.

    Idempotent: calling it for an already-failed/already-deleted row is
    a no-op.
    """
    now = _utc_now_iso()
    truncated = (error_message or "")[:500]
    with get_db_context() as conn:
        conn.execute(
            "UPDATE tts_generations "
            "SET status = 'failed', error_message = ?, completed_at = ? "
            "WHERE id = ? AND status IN ('pending', 'processing')",
            (truncated, now, generation_id),
        )
        conn.commit()


def mark_tts_retry_pending(generation_id, error_message, max_retries):
    """Move a failed attempt back to ``pending`` when retry budget remains.

    ``retry_count`` tracks automatic resumes that have already been queued
    for this row.  Returns the updated row when a retry was reserved, or
    ``None`` when the row vanished, is no longer active, or has exhausted
    its retry budget.
    """
    truncated = (error_message or "")[:500]
    try:
        max_retries = max(0, int(max_retries))
    except (TypeError, ValueError):
        max_retries = 0
    if max_retries <= 0:
        return None

    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE tts_generations "
            "SET status = 'pending', "
            "    retry_count = retry_count + 1, "
            "    started_at = NULL, "
            "    completed_at = NULL, "
            "    filename = NULL, "
            "    file_size = 0, "
            "    error_message = ? "
            "WHERE id = ? "
            "  AND status IN ('pending', 'processing', 'failed') "
            "  AND retry_count < ?",
            (truncated, generation_id, max_retries),
        )
        conn.commit()
        if cur.rowcount <= 0:
            return None
        return conn.execute(
            "SELECT * FROM tts_generations WHERE id = ?",
            (generation_id,),
        ).fetchone()


def mark_tts_rate_limited_pending(generation_id, error_message):
    """Move an active/rate-limited row back to ``pending`` without retry burn.

    Provider rate limits are not page-specific failures.  Keeping the row
    pending preserves the durable queue while the worker backs off globally.
    """
    truncated = (error_message or "")[:500]
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE tts_generations "
            "SET status = 'pending', "
            "    started_at = NULL, "
            "    completed_at = NULL, "
            "    filename = NULL, "
            "    file_size = 0, "
            "    error_message = ? "
            "WHERE id = ? "
            "  AND status IN ('pending', 'processing', 'failed')",
            (truncated, generation_id),
        )
        conn.commit()
        if cur.rowcount <= 0:
            return None
        return conn.execute(
            "SELECT * FROM tts_generations WHERE id = ?",
            (generation_id,),
        ).fetchone()


def delete_tts_generation(page_id):
    """Delete the cached generation row for *page_id*.

    Returns ``(filename, file_size)`` for the deleted row, or ``(None, 0)``
    when no row existed.  Callers are responsible for deleting the on-disk
    MP3 referenced by ``filename`` (if any).
    """
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT filename, file_size FROM tts_generations WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        if not row:
            return None, 0
        conn.execute(
            "DELETE FROM tts_generations WHERE page_id = ?",
            (page_id,),
        )
        conn.commit()
        return row["filename"], row["file_size"]


@retry_on_busy
def list_tts_filenames():
    """Return every non-NULL ``filename`` present in the cache table.

    Used by the cleanup helper that reconciles the on-disk TTS folder
    with the DB so orphaned MP3 files do not accumulate.
    """
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT filename FROM tts_generations "
            "WHERE filename IS NOT NULL"
        ).fetchall()
        return [row["filename"] for row in rows]


@retry_on_busy
def list_tts_generations():
    """Return every ``tts_generations`` row (admin / debugging use)."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM tts_generations ORDER BY requested_at DESC"
        ).fetchall()


@retry_on_busy
def list_tts_generations_with_pages():
    """Return TTS generation rows joined with their parent page metadata.

    Used by the admin TTS status page so each row carries the page title +
    slug for display.  Rows for deleted pages are excluded (their cascade
    delete will have removed them, but we still filter for safety).
    """
    with get_db_context() as conn:
        return conn.execute(
            """
            SELECT g.id            AS id,
                   g.page_id       AS page_id,
                   g.language      AS language,
                   g.status        AS status,
                   g.filename      AS filename,
                   g.file_size     AS file_size,
                   g.requested_at  AS requested_at,
                   g.started_at    AS started_at,
                   g.completed_at  AS completed_at,
                   g.error_message AS error_message,
                   p.slug          AS page_slug,
                   p.title         AS page_title
              FROM tts_generations g
              JOIN pages p ON p.id = g.page_id
             ORDER BY g.requested_at DESC
            """
        ).fetchall()


@retry_on_busy
def count_tts_generations_by_status():
    """Return a dict ``{status: count}`` over every row in ``tts_generations``."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tts_generations GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}


@retry_on_busy
def count_active_tts_generations():
    """Return the number of queued/running TTS rows."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tts_generations "
            "WHERE status IN ('pending', 'processing')"
        ).fetchone()
        return int(row["n"] if row else 0)


@retry_on_busy
def count_active_tts_generations_for_requester(requested_by):
    """Return queued/running TTS rows requested by one logged-in user."""
    if requested_by is None:
        return 0
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tts_generations "
            "WHERE status IN ('pending', 'processing') "
            "  AND requested_by = ?",
            (requested_by,),
        ).fetchone()
        return int(row["n"] if row else 0)


@retry_on_busy
def list_stuck_tts_generations():
    """Return every ``tts_generations`` row whose status is not terminal.

    Only ``pending`` / ``processing`` rows are returned. These are the
    ones that were orphaned by a server restart (the daemon thread that
    owned them no longer exists).  Completed and failed rows are excluded.

    Returns a list of ``sqlite3.Row`` objects (empty list when no stuck
    rows exist).
    """
    with get_db_context() as conn:
        return conn.execute(
            "SELECT g.*, p.slug AS page_slug, p.title AS page_title, "
            "p.content AS page_content "
            "FROM tts_generations g "
            "JOIN pages p ON p.id = g.page_id "
            "WHERE g.status IN ('pending', 'processing')",
        ).fetchall()


@retry_on_busy
def list_pending_tts_generations(limit=1):
    """Return pending TTS rows joined with their parent page metadata.

    Standalone worker processes poll this durable queue.  Multiple workers may
    read the same pending row, but only one can subsequently claim it because
    :func:`mark_tts_processing` performs an atomic ``pending`` -> ``processing``
    update.
    """
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 1
    with get_db_context() as conn:
        return conn.execute(
            "SELECT g.*, p.slug AS page_slug, p.title AS page_title, "
            "p.content AS page_content "
            "FROM tts_generations g "
            "JOIN pages p ON p.id = g.page_id "
            "WHERE g.status = 'pending' "
            "ORDER BY g.requested_at ASC, g.id ASC "
            f"LIMIT {limit}",
        ).fetchall()


@retry_on_busy
def list_resumable_failed_tts_generations(max_retries):
    """Return failed rows that still have automatic retry budget left."""
    try:
        max_retries = max(0, int(max_retries))
    except (TypeError, ValueError):
        max_retries = 0
    if max_retries <= 0:
        return []
    with get_db_context() as conn:
        return conn.execute(
            "SELECT g.*, p.slug AS page_slug, p.title AS page_title, "
            "p.content AS page_content "
            "FROM tts_generations g "
            "JOIN pages p ON p.id = g.page_id "
            "WHERE g.status = 'failed' "
            "  AND (g.retry_count < ? "
            "       OR lower(COALESCE(g.error_message, '')) LIKE '%429%' "
            "       OR lower(COALESCE(g.error_message, '')) LIKE '%too many requests%' "
            "       OR lower(COALESCE(g.error_message, '')) LIKE '%rate limit%' "
            "       OR lower(COALESCE(g.error_message, '')) LIKE '%ratelimit%')",
            (max_retries,),
        ).fetchall()


@retry_on_busy
def count_tts_backfill_candidates():
    """Return active pages that do not currently have usable/in-flight TTS."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n "
            "FROM pages p "
            "LEFT JOIN tts_generations g ON g.page_id = p.id "
            "WHERE p.is_deindexed = 0 "
            "  AND p.pending_deletion = 0 "
            "  AND ("
            "       g.id IS NULL "
            "       OR g.status = 'failed' "
            "       OR (g.status = 'completed' "
            "           AND (g.filename IS NULL OR g.file_size <= 0))"
            "      )"
        ).fetchone()
        return int(row["n"] if row else 0)


@retry_on_busy
def list_tts_backfill_candidates(limit=None):
    """Return active pages that are safe to queue for TTS backfill.

    Pending/processing rows are skipped so a bulk action cannot duplicate live
    work. Completed rows with a usable file are also skipped; malformed
    completed rows and failed rows are considered missing audio and may be
    replaced by callers via :func:`request_tts_generation`.
    """
    sql = (
        "SELECT p.id, p.slug, p.title, p.content "
        "FROM pages p "
        "LEFT JOIN tts_generations g ON g.page_id = p.id "
        "WHERE p.is_deindexed = 0 "
        "  AND p.pending_deletion = 0 "
        "  AND ("
        "       g.id IS NULL "
        "       OR g.status = 'failed' "
        "       OR (g.status = 'completed' "
        "           AND (g.filename IS NULL OR g.file_size <= 0))"
        "      ) "
        "ORDER BY p.id"
    )
    params = ()
    if limit is not None:
        try:
            limit = max(1, min(int(limit), 100000))
        except (TypeError, ValueError):
            limit = None
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    with get_db_context() as conn:
        return conn.execute(sql, params).fetchall()


@retry_on_busy
def reset_to_pending(generation_id):
    """Reset *generation_id* back to ``pending`` and clear ``started_at``.

    Used during startup recovery so the worker state machine can re-enter
    the ``pending`` -> ``processing`` -> ``completed`` path cleanly.
    """
    with get_db_context() as conn:
        conn.execute(
            "UPDATE tts_generations "
            "SET status = 'pending', started_at = NULL, completed_at = NULL "
            "WHERE id = ? AND status IN ('processing', 'pending')",
            (generation_id,),
        )
        conn.commit()


def clear_all_tts_generations():
    """Remove every row in ``tts_generations`` and return the deleted filenames.

    Used by the plugin's on-disable hook so a disabled TTS plugin does not
    leave orphan worker threads or stale rows behind, and by the
    "uninstall + drop data" admin flow.
    """
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT filename FROM tts_generations "
            "WHERE filename IS NOT NULL"
        ).fetchall()
        filenames = [row["filename"] for row in rows]
        conn.execute("DELETE FROM tts_generations")
        conn.commit()
        return filenames
