"""The read-aloud worker: turns queued ``tts_generations`` rows into audio.

Run it as its own process (``python scripts/tts_worker.py``, the systemd unit
``<name>-tts``) or, on small installations, inside the web process with
``BW_TTS_INLINE_WORKER=1``. Either way the same :class:`Worker` runs
``BW_TTS_WORKER_COUNT`` threads that claim jobs from the database, so any
number of workers can share one queue.

The worker never exits because of its environment: while the feature is
switched off, the host disabled generation or no backend is usable (Piper
not installed, GPU server not configured), it idles and checks again every
minute. SIGTERM/SIGINT stop it after the running jobs finish; jobs still
running after ``BW_TTS_SHUTDOWN_GRACE_SECONDS`` go back to the queue.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

from flask import Flask

from ...db import connection_scope
from ...registry import is_enabled
from . import backends, options, service

log = logging.getLogger("bananawiki.tts")

IDLE_INTERVAL = 60.0
HEARTBEAT_INTERVAL = 30.0


def blocked_reason() -> str | None:
    """Why no job may run now (None when the worker may claim jobs)."""
    if not is_enabled("tts"):
        return "the read-aloud feature is switched off"
    if options.host_disabled():
        return "generation is disabled by the host (BW_MANAGED_TTS_DISABLED)"
    return backends.current().problem()


def run_once() -> str | None:
    """Claim and process one job in the current application context.

    Returns the job's outcome, or None when nothing could be run.
    """
    if blocked_reason():
        return None
    job = service.claim()
    if job is None:
        return None
    return service.process(job, backends.current())


class Worker:
    def __init__(self, app: Flask, *, threads: int | None = None, poll_interval: float = 1.0,
                 max_jobs: int | None = None, once: bool = False):
        if threads is None:
            with app.app_context():
                threads = options.config().worker_count
        self.app = app
        self.threads = threads
        self.poll_interval = max(0.1, poll_interval)
        self.max_jobs = max_jobs
        self.once = once
        self.stop = threading.Event()
        self.processed = 0
        self._running: dict[int, int] = {}  # thread ident -> job id
        self._lock = threading.Lock()
        self._last_start = 0.0
        self._last_reason: str | None = None
        self._threads: list[threading.Thread] = []

    # Threads -------------------------------------------------------------

    def start(self, *, daemon: bool = True) -> None:
        for index in range(self.threads):
            thread = threading.Thread(target=self._loop, name=f"bananawiki-tts-{index}", daemon=daemon)
            thread.start()
            self._threads.append(thread)
        if not self.once:
            beat = threading.Thread(target=self._heartbeat, name="bananawiki-tts-lease", daemon=True)
            beat.start()

    def _note_reason(self, reason: str | None) -> None:
        if reason != self._last_reason:
            if reason:
                log.info("Read-aloud worker idle: %s", reason)
            elif self._last_reason:
                log.info("Read-aloud worker resumed")
            self._last_reason = reason

    def _throttle(self) -> None:
        interval = options.config().min_start_interval
        with self._lock:
            wait = self._last_start + interval - time.monotonic()
            self._last_start = max(time.monotonic(), self._last_start + interval)
        if wait > 0:
            self.stop.wait(wait)

    def _budget_left(self) -> bool:
        with self._lock:
            return self.max_jobs is None or self.processed < self.max_jobs

    def _loop(self) -> None:
        ident = threading.get_ident()
        while not self.stop.is_set() and self._budget_left():
            try:
                with self.app.app_context(), connection_scope():
                    reason = blocked_reason()
                    self._note_reason(reason)
                    job = None if reason else service.claim()
                    if job is not None:
                        with self._lock:
                            self._running[ident] = job.id
                        try:
                            self._throttle()
                            outcome = service.process(job, backends.current())
                            log.info("Read-aloud job %s (page %s): %s", job.id, job.page_id, outcome)
                        finally:
                            with self._lock:
                                self._running.pop(ident, None)
                                self.processed += 1
            except Exception:  # noqa: BLE001 - keep the worker alive (database busy, disk full, ...)
                log.exception("Read-aloud worker error")
                job, reason = None, "error"
            if job is not None:
                continue
            if self.once:
                return
            self.stop.wait(IDLE_INTERVAL if reason else self.poll_interval)

    def _heartbeat(self) -> None:
        while not self.stop.wait(HEARTBEAT_INTERVAL):
            try:
                with self.app.app_context(), connection_scope():
                    with self._lock:
                        running = list(self._running.values())
                    service.renew(running)
                    recovered = service.recover_stale()
                    if recovered:
                        log.info("Re-queued %s read-aloud job(s) whose worker stopped", recovered)
            except Exception:  # noqa: BLE001
                log.exception("Read-aloud lease renewal failed")

    # Lifecycle -----------------------------------------------------------

    def alive(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    def recover(self) -> None:
        with self.app.app_context(), connection_scope():
            recovered = service.recover_stale()
        if recovered:
            log.info("Re-queued %s read-aloud job(s) left by a stopped worker", recovered)

    def shutdown(self, grace: float) -> None:
        """Stop claiming, wait up to *grace* seconds, then requeue unfinished jobs."""
        self.stop.set()
        deadline = time.monotonic() + grace
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            unfinished = list(self._running.values())
        if unfinished:
            with self.app.app_context(), connection_scope():
                for job_id in unfinished:
                    service.release(job_id)
            log.info("Returned %s unfinished read-aloud job(s) to the queue", len(unfinished))


# ── Inline worker (inside the web process) ────────────────────────────────────

_inline: dict[int, Worker] = {}
_inline_lock = threading.Lock()


def start_inline(app: Flask) -> None:
    """Start this process's in-app worker once (after any fork)."""
    pid = os.getpid()
    if pid in _inline:
        return
    with _inline_lock:
        if pid in _inline:
            return
        worker = Worker(app)
        worker.start(daemon=True)
        _inline[pid] = worker
        log.info("Read-aloud inline worker started with %s thread(s)", worker.threads)


# ── Standalone process ────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BananaWiki read-aloud worker")
    parser.add_argument("--once", action="store_true", help="process the queued jobs, then exit")
    parser.add_argument("--max-jobs", type=int, default=None, help="exit after this many jobs")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="seconds between queue checks")
    parser.add_argument("--threads", type=int, default=None, help="worker threads (default BW_TTS_WORKER_COUNT)")
    parser.add_argument("--skip-recovery", action="store_true", help="do not requeue jobs of stopped workers")
    parser.add_argument("--tts-folder", help=argparse.SUPPRESS)  # 1.4 flag; the folder is BW_TTS_FOLDER
    return parser.parse_args(argv)


def _build_app(stop: threading.Event, once: bool) -> Flask | None:
    """Create the application, retrying (not crash-looping) while storage is unavailable."""
    from ...app import create_app

    while not stop.is_set():
        try:
            return create_app()
        except Exception:  # noqa: BLE001 - log and retry later instead of exiting
            log.exception("The read-aloud worker cannot start the application")
            if once:
                return None
            stop.wait(IDLE_INTERVAL)
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("BW_TTS_WORKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    if args.tts_folder:
        log.warning("--tts-folder is ignored; set BW_TTS_FOLDER instead")
    stopping = threading.Event()

    def _signal(signum: int, _frame: Any) -> None:
        log.info("Read-aloud worker received signal %s, stopping", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    app = _build_app(stopping, args.once)
    if app is None:
        return 1 if args.once else 0
    threads = min(max(1, args.threads), options.MAX_WORKERS) if args.threads else None
    worker = Worker(app, threads=threads, poll_interval=args.poll_interval, max_jobs=args.max_jobs,
                    once=args.once)
    if not args.skip_recovery:
        worker.recover()
    worker.start(daemon=True)
    log.info("Read-aloud worker running with %s thread(s)", worker.threads)
    while not stopping.is_set() and worker.alive():
        stopping.wait(0.5)
    with app.app_context():
        grace = options.config().shutdown_grace
    worker.shutdown(grace)
    log.info("Read-aloud worker stopped after %s job(s)", worker.processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
