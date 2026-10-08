"""The audio queue: ``tts_generations`` rows and the files they point to.

One row per page (``page_id`` is unique)::

    [no row] -> pending -> processing -> completed
                   ^            |
                   +-- retry ---+-> failed

* Web requests, page events and the admin page only *queue* work
  (:func:`request`). Workers claim rows atomically (:func:`claim`) - several
  worker threads or processes can share the queue.
* A claimed row carries a lease that the worker renews; a row whose lease
  expired (the worker died) goes back to ``pending`` (:func:`recover_stale`).
  The lease is never renewed past ``BW_TTS_MAX_JOB_SECONDS``, and a job that
  ran that long, or whose worker died :data:`MAX_ATTEMPTS` times, fails.
* A failure is retried with exponential backoff (``not_before``) until the
  retry budget is used; a rate-limited backend postpones the job without
  using the budget; permanent errors fail at once.
* Files are named ``tts_<page>_<lang>_<hash8>_<generation>.<ext>`` (the 1.4
  scheme); speed-adjusted downloads add ``.<speed>x.mp3``. Everything that
  removes a row removes its files, and :func:`sweep` deletes any file no row
  references - including those of pages deleted by paths that emit no event.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ....core.timeutil import now_sql, sql_in
from ... import storage
from ...db import db
from ..pages import service as pages
from . import backends, options, text

log = logging.getLogger("bananawiki.tts")

ACTIVE = ("pending", "processing")
LEASE_SECONDS = 120
MAX_ATTEMPTS = 3  # claims that may end without an answer from their worker
ORPHAN_MIN_AGE_SECONDS = 3600
ERROR_MAX = 500


class TtsError(ValueError):
    """A refused request; ``key`` is a translation key."""

    def __init__(self, key: str, status: int = 400):
        super().__init__(key)
        self.key = key
        self.status = status


class SpeedBusy(RuntimeError):
    """Another speed conversion is running in this process."""


@dataclass(frozen=True)
class Outcome:
    row: dict[str, Any] | None
    created: bool
    reason: str | None = None  # already_in_progress, already_generated, queue_full, user_queue_full


@dataclass(frozen=True)
class Job:
    id: int
    page_id: int
    language: str
    content_hash: str
    spoken: str
    retry_count: int


@dataclass(frozen=True)
class PageAudio:
    row: dict[str, Any] | None
    usable: bool
    in_flight: bool


# ── Reading ───────────────────────────────────────────────────────────────────


def generation(page_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM tts_generations WHERE page_id = ?", (page_id,))


def generation_by_id(generation_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM tts_generations WHERE id = ?", (generation_id,))


def audio_path(row: dict[str, Any] | None) -> Path | None:
    if not row or row["status"] != "completed" or not row["filename"] or not row["file_size"]:
        return None
    if os.path.basename(row["filename"]) != row["filename"]:
        return None
    return storage.resolve("tts", row["filename"])


def page_audio(page: dict[str, Any]) -> PageAudio:
    """The page's audio state. Completed audio of older content counts as missing."""
    row = generation(page["id"])
    if row is None:
        return PageAudio(None, False, False)
    if row["status"] in ACTIVE:
        return PageAudio(row, False, True)
    usable = audio_path(row) is not None and row["content_hash"] == text.content_hash(
        text.spoken_text(page.get("title"), page.get("content")), row["language"])
    return PageAudio(row, usable, False)


def serialize(row: dict[str, Any] | None, *, usable: bool = False) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "language": row["language"],
        "language_label": text.LANGUAGE_LABELS.get(row["language"], row["language"]),
        "status": row["status"],
        "file_size": row["file_size"] or 0,
        "retry_count": row["retry_count"] or 0,
        "requested_at": row["requested_at"],
        "completed_at": row["completed_at"],
        "has_file": usable,
    }


# ── Queueing ──────────────────────────────────────────────────────────────────


def _chosen_language(language: str | None) -> str | None:
    """The enabled code for an explicit *language*, None for automatic detection."""
    if not language or language == "auto":
        return None
    chosen = text.normalize_language(language, allowed=options.enabled_languages())
    if chosen is None:
        raise TtsError("tts.error.unsupported_language")
    return chosen


def plan(page: dict[str, Any], language: str | None = None) -> tuple[str, str, str] | None:
    """``(spoken, language, hash)`` for *page*, or None when it has nothing to read."""
    chosen = _chosen_language(language)
    spoken = text.spoken_text(page.get("title"), page.get("content"))
    if not spoken.strip():
        return None
    if chosen is None:
        chosen = text.detect_language(spoken, allowed=options.enabled_languages())
    return spoken, chosen, text.content_hash(spoken, chosen)


def _usable_row(row: dict[str, Any]) -> bool:
    return row["status"] == "completed" and audio_path(row) is not None


def _limit_reason(limits: tuple[int, int], requested_by: str | None) -> str | None:
    """``queue_full`` or ``user_queue_full`` when *limits* leave no room for another job."""
    total, per_user = limits
    if db.scalar("SELECT COUNT(*) FROM tts_generations WHERE status IN ('pending', 'processing')",
                 default=0) >= total:
        return "queue_full"
    if requested_by and db.scalar("SELECT COUNT(*) FROM tts_generations WHERE status IN ('pending', 'processing') "
                                  "AND requested_by = ?", (requested_by,), default=0) >= per_user:
        return "user_queue_full"
    return None


def _refused_early(page_id: int, requested_by: str | None, limits: tuple[int, int]) -> Outcome | None:
    """The answer to a reader's request when it does not depend on the page text.

    Runs before the page is normalised, so a refused request costs a few
    queries; :func:`request` checks everything again in its transaction.
    """
    existing = generation(page_id)
    if existing is not None and existing["status"] in ACTIVE:
        return Outcome(existing, False, "already_in_progress")
    if existing is not None and _usable_row(existing):
        return None  # whether it is still current ("already_generated") needs the text
    reason = _limit_reason(limits, requested_by)
    return Outcome(existing, False, reason) if reason else None


def request(page: dict[str, Any], *, requested_by: str | None, language: str | None = None,
            limits: tuple[int, int] | None = None) -> Outcome:
    """Queue audio for *page*.

    Without *limits* (page events, backfill) an identical pending, running or
    completed job is kept and anything else is replaced. With *limits*
    (``(total, per_user)``, readers pressing "Generate") an existing job
    always wins and the number of active jobs is capped.
    """
    if limits:
        _chosen_language(language)  # an unsupported language is still refused first
        refused = _refused_early(page["id"], requested_by, limits)
        if refused is not None:
            return refused
    planned = plan(page, language)
    if planned is None:
        raise TtsError("tts.error.empty_page")
    spoken, chosen, digest = planned
    replaced: str | None = None
    with db.transaction():
        existing = generation(page["id"])
        if existing:
            same = existing["content_hash"] == digest and existing["language"] == chosen
            if existing["status"] in ACTIVE and (limits or same):
                return Outcome(existing, False, "already_in_progress")
            fresh = existing["content_hash"] == text.content_hash(spoken, existing["language"])
            if _usable_row(existing) and (same or (limits and fresh)):
                return Outcome(existing, False, "already_generated")
        if limits:
            reason = _limit_reason(limits, requested_by)
            if reason:
                return Outcome(existing, False, reason)
        if existing:
            replaced = existing["filename"]
            db.execute("DELETE FROM tts_generations WHERE id = ?", (existing["id"],))
        db.insert("tts_generations", {
            "page_id": page["id"], "language": chosen, "status": "pending", "content_hash": digest,
            "requested_by": requested_by, "requested_at": now_sql(),
        })
        row = generation(page["id"])
    remove_files(replaced)
    return Outcome(row, True)


def manual_limits() -> tuple[int, int]:
    cfg = options.config()
    return cfg.manual_max_active_jobs, cfg.manual_max_active_per_user


def queue_state(user: dict[str, Any] | None) -> str | None:
    """Why a reader cannot start a generation right now (None: they can)."""
    if options.host_disabled():
        return "disabled_by_host"
    return _limit_reason(manual_limits(), user["id"] if user else None)


# ── Removing ──────────────────────────────────────────────────────────────────


def folder() -> Path:
    return storage.folder_path("tts")


def _variants(filename: str) -> list[str]:
    stem = filename.rsplit(".", 1)[0]
    try:
        return [name for name in os.listdir(folder()) if name.startswith(stem + ".") and name != filename]
    except OSError:
        return []


def remove_files(filename: str | None) -> None:
    """Delete an audio file and its speed variants."""
    if not filename or os.path.basename(filename) != filename:
        return
    for name in [filename, *_variants(filename)]:
        storage.delete("tts", name)


def remove_page_files(page_id: int) -> None:
    prefix = f"tts_{int(page_id)}_"
    try:
        names = [name for name in os.listdir(folder()) if name.startswith(prefix)]
    except OSError:
        return
    for name in names:
        storage.delete("tts", name)


def delete_generation(row: dict[str, Any]) -> None:
    db.execute("DELETE FROM tts_generations WHERE id = ?", (row["id"],))
    remove_files(row["filename"])


def delete_for_page(page_id: int) -> bool:
    row = generation(page_id)
    if row is None:
        return False
    delete_generation(row)
    return True


def clear_all() -> int:
    """Delete every row and every audio file (admin action)."""
    with db.transaction():
        count = db.scalar("SELECT COUNT(*) FROM tts_generations", default=0)
        db.execute("DELETE FROM tts_generations")
    try:
        names = [name for name in os.listdir(folder()) if name.startswith(("tts_", ".part-"))]
    except OSError:
        names = []
    for name in names:
        storage.delete("tts", name)
    return int(count)


# ── Page events ───────────────────────────────────────────────────────────────


def on_page_created(page: dict[str, Any], author_id: str | None = None, **_: Any) -> None:
    if options.auto_generate() and not options.host_disabled():
        _queue_quietly(pages.get(page["id"]), author_id)


def on_page_updated(page: dict[str, Any], author_id: str | None = None, **_: Any) -> None:
    """Drop audio of the old text; queue new audio when it had some or auto mode is on."""
    current = pages.get(page["id"])
    if current is None:
        return
    row = generation(current["id"])
    if row is not None:
        spoken = text.spoken_text(current.get("title"), current.get("content"))
        if row["status"] != "failed" and row["content_hash"] == text.content_hash(spoken, row["language"]):
            return
    had_audio = row is not None and (row["status"] in ACTIVE or _usable_row(row))
    wanted = (had_audio or options.auto_generate()) and not options.host_disabled()
    if wanted and _queue_quietly(current, author_id):
        return
    if row is not None:
        delete_generation(row)


def on_page_deleted(page: dict[str, Any], **_: Any) -> None:
    """The row goes with the page (ON DELETE CASCADE); the files go here."""
    db.execute("DELETE FROM tts_generations WHERE page_id = ?", (page["id"],))
    remove_page_files(page["id"])


def _queue_quietly(page: dict[str, Any] | None, requested_by: str | None) -> bool:
    if page is None:
        return False
    try:
        return request(page, requested_by=requested_by).row is not None
    except TtsError:
        return False


# ── Admin ─────────────────────────────────────────────────────────────────────

_CANDIDATES = (
    "FROM pages p LEFT JOIN tts_generations g ON g.page_id = p.id "
    "WHERE p.is_deindexed = 0 AND p.pending_deletion = 0 "
    "AND (g.id IS NULL OR g.status = 'failed' OR (g.status = 'completed' AND (g.filename IS NULL OR g.file_size <= 0)))"
)


def stats() -> dict[str, Any]:
    counts = {row["status"]: row["n"] for row in db.all(
        "SELECT status, COUNT(*) AS n FROM tts_generations GROUP BY status")}
    total = sum(counts.values())
    return {
        "counts": {status: counts.get(status, 0) for status in ("pending", "processing", "completed", "failed")},
        "total": total,
        "completion": round(counts.get("completed", 0) * 100 / total) if total else 0,
        "bytes": db.scalar("SELECT COALESCE(SUM(file_size), 0) FROM tts_generations WHERE status = 'completed'",
                           default=0),
        "pages": db.scalar("SELECT COUNT(*) FROM pages WHERE is_deindexed = 0 AND pending_deletion = 0", default=0),
        "missing": db.scalar(f"SELECT COUNT(*) {_CANDIDATES}", default=0),
    }


def queue(*, status: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    where, params = "", []
    if status in ("pending", "processing", "completed", "failed"):
        where, params = "WHERE g.status = ?", [status]
    total = db.scalar(f"SELECT COUNT(*) FROM tts_generations g {where}", params, default=0)
    rows = db.all(
        "SELECT g.*, p.slug AS page_slug, p.title AS page_title, u.username AS requested_by_name "
        f"FROM tts_generations g JOIN pages p ON p.id = g.page_id LEFT JOIN users u ON u.id = g.requested_by {where} "
        "ORDER BY CASE g.status WHEN 'processing' THEN 0 WHEN 'pending' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END, "
        "g.requested_at DESC, g.id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return rows, int(total)


def backfill(requested_by: str | None) -> dict[str, int]:
    """Queue every visible page that has no usable or queued audio."""
    result = {"queued": 0, "empty": 0, "skipped": 0}
    for page_id in db.column(f"SELECT p.id {_CANDIDATES} ORDER BY p.id"):
        page = pages.get(page_id)
        if page is None:
            continue
        try:
            outcome = request(page, requested_by=requested_by)
        except TtsError:
            result["empty"] += 1
            continue
        result["queued" if outcome.created else "skipped"] += 1
    return result


def retry(row: dict[str, Any], requested_by: str | None) -> bool:
    """Queue a failed job again (fresh budget), or run a waiting one now."""
    if row["status"] == "pending":
        db.execute("UPDATE tts_generations SET not_before = NULL WHERE id = ? AND status = 'pending'", (row["id"],))
        return True
    if row["status"] != "failed":
        return False
    page = pages.get(row["page_id"])
    if page is None:
        return False
    try:
        with db.transaction():
            delete_generation(row)
            request(page, requested_by=requested_by, language=row["language"])
    except TtsError:
        return False
    return True


def retry_failed(requested_by: str | None) -> int:
    rows = db.all("SELECT * FROM tts_generations WHERE status = 'failed'")
    return sum(1 for row in rows if retry(row, requested_by))


# ── Worker side ───────────────────────────────────────────────────────────────


_RUNNABLE = "g.status = 'pending' AND (g.not_before IS NULL OR g.not_before <= ?)"


def claim(scan: int = 10) -> Job | None:
    """Atomically take the oldest runnable pending job (or None).

    The page is normalised before the write transaction, which only checks
    that the job is still pending and the page still has the text that was
    read: a long page never holds the database's write lock. The claim
    counts as an attempt until the worker reports back (see
    :func:`recover_stale`).
    """
    now = now_sql()
    for job_id in db.column(f"SELECT g.id FROM tts_generations g WHERE {_RUNNABLE} "
                            "ORDER BY g.requested_at, g.id LIMIT ?", (now, scan)):
        page = db.one("SELECT p.title, p.content FROM tts_generations g JOIN pages p ON p.id = g.page_id "
                      "WHERE g.id = ?", (job_id,))
        if page is None:
            continue
        spoken = text.spoken_text(page["title"], page["content"])
        with db.transaction():
            row = db.one(
                "SELECT g.page_id, g.language, g.retry_count FROM tts_generations g JOIN pages p ON p.id = g.page_id "
                f"WHERE g.id = ? AND {_RUNNABLE} AND p.title IS ? AND p.content IS ?",
                (job_id, now, page["title"], page["content"]),
            )
            if row is None:
                continue  # claimed by another worker, replaced or edited meanwhile
            if not spoken.strip():
                db.execute("DELETE FROM tts_generations WHERE id = ?", (job_id,))
                continue
            digest = text.content_hash(spoken, row["language"])
            db.execute(
                "UPDATE tts_generations SET status = 'processing', started_at = ?, lease_until = ?, "
                "content_hash = ?, completed_at = NULL, attempts = attempts + 1 WHERE id = ? AND status = 'pending'",
                (now, sql_in(seconds=LEASE_SECONDS), digest, job_id),
            )
            return Job(job_id, row["page_id"], row["language"], digest, spoken, int(row["retry_count"] or 0))
    return None


def renew(job_ids: list[int]) -> None:
    """Extend the leases of running jobs, never past ``BW_TTS_MAX_JOB_SECONDS`` after their start."""
    if job_ids:
        marks = ",".join("?" for _ in job_ids)
        lease = sql_in(seconds=LEASE_SECONDS)
        db.execute(
            "UPDATE tts_generations SET lease_until = MIN(?, COALESCE(datetime(started_at, ?), ?)) "
            f"WHERE status = 'processing' AND id IN ({marks})",
            (lease, f"+{int(options.config().max_job_seconds)} seconds", lease, *job_ids),
        )


def final_filename(job: Job, extension: str) -> str:
    ext = extension if extension in ("mp3", "wav") else "mp3"
    return f"tts_{job.page_id}_{job.language}_{job.content_hash[:8]}_{job.id}.{ext}"


def complete(job: Job, filename: str, size: int) -> bool:
    """Record the finished file; False when the job was cancelled or replaced meanwhile."""
    cursor = db.execute(
        "UPDATE tts_generations SET status = 'completed', filename = ?, file_size = ?, completed_at = ?, "
        "error_message = NULL, lease_until = NULL, not_before = NULL WHERE id = ? AND status = 'processing'",
        (filename, size, now_sql(), job.id),
    )
    return cursor.rowcount > 0


def retry_delay(retry_count: int) -> float:
    cfg = options.config()
    if cfg.resume_base_delay <= 0:
        return 0.0
    delay = cfg.resume_base_delay * (2 ** max(0, retry_count - 1))
    return min(delay, cfg.resume_max_delay) if cfg.resume_max_delay > 0 else delay


# A worker that reports back gives its claim back: only claims that ended
# without an answer (the worker died) stay counted in ``attempts``.
_ANSWERED = "attempts = MAX(attempts - 1, 0)"


def fail(job: Job, error: backends.SynthesisError) -> str:
    """Record a failure: ``"cooldown"``, ``"retry"`` or ``"failed"``."""
    message = str(error)[:ERROR_MAX]
    cfg = options.config()
    if error.rate_limited:
        cursor = db.execute(
            f"UPDATE tts_generations SET status = 'pending', started_at = NULL, lease_until = NULL, {_ANSWERED}, "
            "error_message = ?, not_before = ? WHERE id = ? AND status = 'processing'",
            (message, sql_in(seconds=cfg.rate_limit_cooldown), job.id),
        )
        return "cooldown" if cursor.rowcount else "failed"
    if error.retryable and job.retry_count < cfg.max_auto_resume_attempts:
        cursor = db.execute(
            "UPDATE tts_generations SET status = 'pending', retry_count = retry_count + 1, started_at = NULL, "
            f"lease_until = NULL, {_ANSWERED}, error_message = ?, not_before = ? WHERE id = ? AND status = 'processing'",
            (message, sql_in(seconds=retry_delay(job.retry_count + 1)), job.id),
        )
        if cursor.rowcount:
            return "retry"
    db.execute(
        "UPDATE tts_generations SET status = 'failed', error_message = ?, completed_at = ?, lease_until = NULL "
        "WHERE id = ? AND status = 'processing'",
        (message, now_sql(), job.id),
    )
    return "failed"


def release(job_id: int) -> None:
    """Give a job back to the queue (worker shutting down)."""
    db.execute(
        f"UPDATE tts_generations SET status = 'pending', started_at = NULL, lease_until = NULL, {_ANSWERED} "
        "WHERE id = ? AND status = 'processing'", (job_id,),
    )


_EXPIRED = "status = 'processing' AND COALESCE(lease_until, datetime(started_at, ?), requested_at) < ?"
_FAIL_STALE = "UPDATE tts_generations SET status = 'failed', error_message = ?, completed_at = ?, lease_until = NULL "


def recover_stale() -> int:
    """Hand jobs whose worker stopped renewing its lease back to the queue.

    Two kinds fail instead, because running them again would only repeat
    what went wrong: a job whose lease ran out at ``BW_TTS_MAX_JOB_SECONDS``
    (:func:`renew` stops there) took too long, and a job claimed
    :data:`MAX_ATTEMPTS` times without an answer keeps stopping its worker
    (out of memory, a crash in the speech engine). Returns the number of
    jobs put back in the queue.
    """
    now = now_sql()
    expired = (f"+{LEASE_SECONDS} seconds", now)
    seconds = int(options.config().max_job_seconds)
    too_long = db.execute(
        f"{_FAIL_STALE} WHERE {_EXPIRED} AND lease_until >= datetime(started_at, ?)",
        (f"The job ran longer than {seconds} seconds (BW_TTS_MAX_JOB_SECONDS).", now, *expired,
         f"+{seconds} seconds"),
    ).rowcount
    lost = db.execute(
        f"{_FAIL_STALE} WHERE {_EXPIRED} AND attempts >= ?",
        (f"The worker stopped {MAX_ATTEMPTS} times while reading this page (for example it ran out of "
         "memory), so the job is not retried automatically.", now, *expired, MAX_ATTEMPTS),
    ).rowcount
    if too_long or lost:
        log.warning("Read-aloud: %s job(s) ran too long and %s stopped their worker too often; marked failed",
                    too_long, lost)
    return db.execute(
        f"UPDATE tts_generations SET status = 'pending', started_at = NULL, lease_until = NULL WHERE {_EXPIRED}",
        expired,
    ).rowcount


def process(job: Job, synthesizer: backends.Synthesizer) -> str:
    """Synthesise one claimed job; returns ``completed``, ``cancelled``, ``retry``, ``cooldown`` or ``failed``."""
    if not storage.quota_allows(0):
        return fail(job, backends.SynthesisError("The storage quota of this wiki is full.", retryable=False))
    target_dir = folder()
    try:
        audio = synthesizer.synthesize(job.spoken, job.language, target_dir)
    except backends.SynthesisError as error:
        log.warning("Speech for page %s failed: %s", job.page_id, error)
        return fail(job, error)
    except Exception as error:  # noqa: BLE001 - an unexpected backend error fails the job, not the worker
        log.exception("Speech backend crashed on page %s", job.page_id)
        return fail(job, backends.SynthesisError(f"Unexpected error: {error}"))
    filename = final_filename(job, audio.extension)
    target = target_dir / filename
    try:
        os.replace(audio.path, target)
        size = target.stat().st_size
    except OSError as error:
        audio.path.unlink(missing_ok=True)
        return fail(job, backends.SynthesisError(f"Cannot store the audio: {error}"))
    if not complete(job, filename, size):
        target.unlink(missing_ok=True)
        return "cancelled"
    return "completed"


# ── Maintenance ───────────────────────────────────────────────────────────────


def sweep() -> dict[str, int]:
    """Delete unreferenced files and refresh completed audio of edited pages."""
    result = {"orphans": 0, "refreshed": 0}
    rows = db.all("SELECT g.*, p.title, p.content FROM tts_generations g JOIN pages p ON p.id = g.page_id "
                  "WHERE g.status = 'completed'")
    for row in rows:
        spoken = text.normalize_text(row["title"], row["content"])
        if row["content_hash"] != text.content_hash(spoken, row["language"]):
            page = pages.get(row["page_id"])
            if page is None or options.host_disabled() or not _queue_quietly(page, None):
                delete_generation(row)
            result["refreshed"] += 1
    referenced = db.column("SELECT filename FROM tts_generations WHERE filename IS NOT NULL")
    keep = set(referenced)
    stems = tuple(name.rsplit(".", 1)[0] + "." for name in referenced)
    cutoff = time.time() - ORPHAN_MIN_AGE_SECONDS
    try:
        entries = list(os.scandir(folder()))
    except OSError:
        return result
    for entry in entries:
        if not entry.is_file() or entry.name in keep or (stems and entry.name.startswith(stems)):
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        storage.delete("tts", entry.name)
        result["orphans"] += 1
    return result


_speed_lock = threading.Semaphore(1)


def speed_variant(row: dict[str, Any], speed: float) -> Path | None:
    """The cached MP3 of *row* at *speed*, encoded once on first request.

    Returns None when ffmpeg is missing. Raises :class:`SpeedBusy` when this
    process is already encoding another variant, so a burst of requests can
    never run many ffmpeg processes at once.
    """
    source = audio_path(row)
    if source is None or not backends.ffmpeg_available():
        return None
    name = f"{row['filename'].rsplit('.', 1)[0]}.{text.speed_tag(speed)}x.mp3"
    existing = storage.resolve("tts", name)
    if existing is not None:
        return existing
    if not _speed_lock.acquire(blocking=False):
        raise SpeedBusy()
    try:
        target_dir = folder()
        partial = target_dir / f".part-{os.getpid()}-{threading.get_ident()}.mp3"
        try:
            backends.encode_mp3(source, partial, speed=speed)
            os.replace(partial, target_dir / name)
        except (backends.SynthesisError, OSError) as error:
            partial.unlink(missing_ok=True)
            log.warning("Speed conversion failed for %s: %s", row["filename"], error)
            return None
    finally:
        _speed_lock.release()
    return storage.resolve("tts", name)
