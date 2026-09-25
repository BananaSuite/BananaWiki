#!/usr/bin/env python3
"""I/O health monitor for BananaWiki's SQLite database directory.

Goal: when an instance starts returning ``disk I/O error`` from SQLite,
this script tells you in plain English whether it's the **storage**
(network volume stall, dying disk, kernel I/O error) or the
**filesystem** (wrong ownership / permissions on the WAL sidecar
files), so you can take it to Hetzner support with evidence or fix it
yourself in 30 seconds.

What it checks, in a loop:

1.  **Permissions** -- can the current user write to the DB directory,
    the DB file itself, and the existing ``-wal`` / ``-shm`` sidecar
    files?  This is the #1 cause of intermittent SQLite IOERR when free
    space is fine -- a backup or deploy script left a sidecar file
    owned by root and the gunicorn user can't write to it.

2.  **Latency** -- how long does an ``fsync()`` against a tiny file in
    the same directory take?  Anything over ``--slow-threshold-ms``
    (default 250 ms) is flagged.  Spikes of 500 ms+ on a Hetzner Cloud
    volume mean Ceph is having a bad day; sustained 50-200 ms is
    "noisy neighbor" territory.

3.  **Hard I/O errors** -- if any ``open`` / ``read`` / ``write`` /
    ``fsync`` actually fails, that's a real kernel-level event.  We
    capture the OSError code (EIO, EROFS, EACCES, ENOSPC, ...) so you
    know exactly what to fix.

4.  **dmesg correlation** -- tails the kernel ring buffer for new
    ``EXT4-fs error``, ``nvme: ...``, ``blk_update_request: I/O
    error``, and ``Buffer I/O error`` lines, which are unambiguous
    proof of a hardware/storage problem (paste these into your Hetzner
    support ticket and they will RMA / migrate you).

5.  **SMART** (optional) -- when ``smartctl`` is installed and the DB
    sits on a block device we can resolve, the script periodically
    captures ``Reallocated_Sector_Ct`` / ``Current_Pending_Sector`` /
    ``Media_Wearout_Indicator``.  Non-zero rising values mean the
    drive is dying -- relevant only on Hetzner Dedicated; Cloud
    volumes don't expose SMART.

6.  **SQLite integrity** (one-shot, on start) -- runs ``PRAGMA
    integrity_check`` against the configured DB so you can rule out
    on-disk corruption as the trigger.

Output is two streams:

* ``stdout`` -- a one-line summary every ``--interval`` seconds, easy
  to ``tail -f`` in a tmux pane while you reproduce the bug.
* ``--log`` -- the same data plus full tracebacks and kernel lines as
  newline-delimited JSON, suitable for attaching to a Hetzner ticket.

Quick start::

    # 1. As the gunicorn user (so permission tests are realistic):
    sudo -u www-data python3 scripts/io_health_check.py \\
        --db-dir /opt/BananaWiki/instances/wiki__apex \\
        --log /var/log/bananawiki-iocheck.log \\
        --interval 5

    # 2. Reproduce the 500s.  When they happen, attach the log.

    # 3. To run it continuously as a systemd service, see the unit
    #    file emitted by ``--print-systemd-unit``.

Exit codes: 0 normal termination; 1 startup error (bad arguments, DB
path unreadable); 2 the script saw enough hard errors during the run
to be sure something is broken (paste the log into a ticket).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for log lines."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class _Stats:
    """Running counters used for the periodic summary line."""

    started_at: float = field(default_factory=time.time)
    samples: int = 0
    slow_fsyncs: int = 0
    io_errors: int = 0
    perm_errors: int = 0
    max_fsync_ms: float = 0.0
    last_dmesg_marker: int = 0  # bytes already consumed from dmesg


class _Logger:
    """Tiny tee-style logger: one stream of human-readable summary lines,
    plus an optional JSON-lines audit log for later inspection."""

    def __init__(self, log_path: Optional[str], verbose: bool):
        self.log_path = log_path
        self.verbose = verbose
        self._fp = None
        if log_path:
            # ``buffering=1`` enables line buffering so ``tail -f`` sees
            # each event as it happens; the file is appended to so the
            # script can be restarted without losing prior history.
            self._fp = open(log_path, "a", buffering=1)

    def event(self, level: str, **fields) -> None:
        """Emit a structured event to the JSON log (and console if verbose)."""
        payload = {"ts": _now(), "level": level, **fields}
        line = json.dumps(payload, default=str)
        if self._fp:
            self._fp.write(line + "\n")
        if self.verbose or level in ("error", "warning"):
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def summary(self, msg: str) -> None:
        """Emit a one-line human summary to stdout (always)."""
        sys.stdout.write(f"[{_now()}] {msg}\n")
        sys.stdout.flush()

    def close(self) -> None:
        if self._fp:
            self._fp.close()


def _check_permissions(db_dir: str, db_file: str, logger: _Logger) -> bool:
    """Return True iff the current process can write to the directory
    and to the DB / WAL / SHM sidecar files.

    SQLite needs *all four* paths to be writable: the directory (for
    WAL rollovers and journal files), the main DB, and both sidecars
    if they already exist.  A common production failure is that a
    backup or deploy script left ``-wal`` or ``-shm`` owned by ``root``
    while the gunicorn user is ``www-data`` -- normal SELECTs still
    work for a while (read from page cache), then WAL needs to rotate
    and the next write raises ``disk I/O error``.
    """
    ok = True

    # 1. Directory must be writable so SQLite can create journal files.
    if not os.path.isdir(db_dir):
        logger.event("error", check="permissions", path=db_dir,
                     reason="not a directory")
        return False
    if not os.access(db_dir, os.W_OK | os.X_OK):
        st = os.stat(db_dir)
        logger.event(
            "error", check="permissions", path=db_dir,
            reason="directory not writable by current user",
            mode=stat.filemode(st.st_mode), owner_uid=st.st_uid,
            owner_gid=st.st_gid, current_uid=os.getuid(),
        )
        ok = False

    # 2. DB file + sidecars (-wal, -shm) must each be writable if present.
    for suffix in ("", "-wal", "-shm"):
        path = db_file + suffix
        if not os.path.exists(path):
            continue  # sidecar may not exist yet -- that's fine
        if not os.access(path, os.R_OK | os.W_OK):
            st = os.stat(path)
            logger.event(
                "error", check="permissions", path=path,
                reason="file not read+write by current user",
                mode=stat.filemode(st.st_mode), owner_uid=st.st_uid,
                owner_gid=st.st_gid, current_uid=os.getuid(),
            )
            ok = False
    return ok


def _probe_fsync_latency(db_dir: str, logger: _Logger,
                         slow_threshold_ms: float) -> tuple[Optional[float], Optional[str]]:
    """Write a tiny file, fsync it, time the syscall; return (ms, error).

    The intent is to mimic what SQLite does when it commits a
    transaction: open a file in the DB directory, write a small page,
    fsync to durable storage.  ``ms`` is None on hard failure; in that
    case ``error`` carries the ``errno`` name (EIO, EROFS, EACCES,
    ENOSPC, ...) for the structured log.
    """
    # Use a unique filename so concurrent monitors don't fight.
    probe_path = os.path.join(
        db_dir, f".iocheck-{os.getpid()}-{random.randint(0, 1 << 30):x}.tmp",
    )
    payload = os.urandom(4096)  # one SQLite page (default).
    fd = None
    started = time.perf_counter()
    try:
        # O_CREAT | O_WRONLY | O_TRUNC matches what sqlite3.c does for
        # the rollback journal.  Pass mode 0o600 so we don't widen
        # permissions on the directory.
        fd = os.open(probe_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
    except OSError as exc:
        # Capture the errno name -- "errno 5" alone is hard to grep;
        # ``errno.EIO`` etc. is unambiguous in support tickets.
        try:
            import errno
            errname = errno.errorcode.get(exc.errno, str(exc.errno))
        except Exception:
            errname = str(exc.errno)
        logger.event("error", check="fsync", path=probe_path,
                     errno=errname, message=str(exc))
        return None, errname
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(probe_path)
        except OSError:
            pass

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if elapsed_ms >= slow_threshold_ms:
        logger.event("warning", check="fsync", elapsed_ms=round(elapsed_ms, 2),
                     threshold_ms=slow_threshold_ms,
                     note="fsync slower than threshold -- likely storage stall")
    return elapsed_ms, None


def _read_new_dmesg(stats: _Stats, logger: _Logger) -> None:
    """Capture new I/O-error lines from ``dmesg``, if accessible.

    ``dmesg`` is unprivileged on most distributions (``kernel.dmesg_restrict``
    is 0), so this typically just works.  When the kernel ring buffer is
    locked down we silently skip -- the rest of the script still works.
    Lines of interest: ``blk_update_request: I/O error`` (block layer),
    ``Buffer I/O error`` (page cache), ``EXT4-fs error`` (filesystem
    layer), ``nvme: ...`` (NVMe firmware), ``sd[a-z]: ...`` (SCSI).
    """
    # Match only lines that unambiguously indicate a problem.  Bare
    # ``ext4-fs`` / ``nvme`` are too broad -- the kernel logs normal mount
    # events with those prefixes, which would create false positives.
    interesting = (
        "i/o error",
        "buffer i/o error",
        "blk_update_request",
        "ext4-fs error",
        "ext4-fs warning",
        "remounting filesystem read-only",
        "remounted read-only",
        "task blocked for more than",
        "nvme: i/o",
        "nvme nvme",      # NVMe controller fault messages
        "ata bus error",
        "medium error",
    )
    try:
        proc = subprocess.run(
            ["dmesg", "-T", "--nopager"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return
    if proc.returncode != 0:
        return
    output = proc.stdout
    # Naive but reliable: skip the prefix we've already seen and report
    # only the new tail.  ``stats.last_dmesg_marker`` stores the byte
    # offset of the last successful scan.
    new = output[stats.last_dmesg_marker:]
    stats.last_dmesg_marker = len(output)
    for line in new.splitlines():
        low = line.lower()
        if any(token in low for token in interesting):
            stats.io_errors += 1
            logger.event("error", check="dmesg", line=line.strip())


def _read_smart_once(db_dir: str, logger: _Logger) -> None:
    """Capture relevant SMART counters from the underlying block device.

    Best-effort: requires ``smartctl`` and root.  Skips silently if the
    device can't be resolved (Hetzner Cloud volumes don't expose SMART
    in a meaningful way; the host hides the underlying Ceph layer).
    """
    if not shutil.which("smartctl"):
        return
    try:
        # ``df`` is the simplest way to resolve a path to a device.
        df = subprocess.run(
            ["df", "--output=source", db_dir],
            capture_output=True, text=True, timeout=5, check=False,
        )
        lines = [l for l in df.stdout.splitlines() if l.startswith("/dev/")]
        if not lines:
            return
        device = lines[0].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    try:
        proc = subprocess.run(
            ["smartctl", "-A", "-H", device],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return
    # Surface just the interesting SMART attributes -- the full output
    # is captured in the JSON log too.
    interesting = (
        "Reallocated_Sector_Ct", "Current_Pending_Sector",
        "Offline_Uncorrectable", "Media_Wearout_Indicator",
        "Percent_Lifetime_Remain", "SMART overall-health",
    )
    summary = [
        line.strip()
        for line in proc.stdout.splitlines()
        if any(tok in line for tok in interesting)
    ]
    logger.event(
        "info", check="smart", device=device, summary=summary,
        rc=proc.returncode,
    )


def _run_integrity_check(db_file: str, logger: _Logger) -> None:
    """Run ``PRAGMA integrity_check`` once to rule out on-disk corruption.

    On a small (< 1 GiB) DB this completes in well under a second.  We
    only run it once on start because it's not free; the periodic
    fsync probe is the cheap recurring check.
    """
    if not os.path.exists(db_file):
        logger.event("warning", check="integrity",
                     reason=f"DB file not found at {db_file}")
        return
    try:
        with sqlite3.connect(db_file, timeout=5) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        result = row[0] if row else "(no result)"
        level = "info" if result == "ok" else "error"
        logger.event(level, check="integrity", result=result)
    except sqlite3.DatabaseError as exc:
        logger.event("error", check="integrity", error=str(exc))


#  systemd unit emitter


_SYSTEMD_UNIT_TEMPLATE = """\
# /etc/systemd/system/bananawiki-iocheck.service
#
# Long-running I/O health monitor for the BananaWiki SQLite DB.
# Logs to /var/log/bananawiki-iocheck.log -- attach that file to your
# Hetzner support ticket if you suspect a storage problem.
#
# Install:
#   sudo cp bananawiki-iocheck.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now bananawiki-iocheck.service
[Unit]
Description=BananaWiki SQLite I/O health monitor
After=network.target

[Service]
Type=simple
# Run as the same user that gunicorn uses so permission checks are
# realistic.  Replace ``www-data`` with whatever ``ps`` shows for the
# gunicorn workers (often ``bananawiki`` or ``www-data``).
User={service_user}
Group={service_user}
ExecStart=/usr/bin/python3 {script_path} \\
    --db-dir {db_dir} \\
    --log /var/log/bananawiki-iocheck.log \\
    --interval 5
Restart=on-failure
RestartSec=30
# Limit resource usage -- the script itself does very little I/O,
# but capping memory is a courtesy on a shared host.
MemoryMax=64M
CPUQuota=10%

[Install]
WantedBy=multi-user.target
"""


def _emit_systemd_unit(db_dir: str) -> None:
    """Print a ready-to-install systemd unit file to stdout."""
    script_path = os.path.abspath(sys.argv[0])
    # Best guess for the gunicorn user; the operator should verify.
    service_user = "www-data"
    print(_SYSTEMD_UNIT_TEMPLATE.format(
        script_path=script_path, db_dir=db_dir, service_user=service_user,
    ))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Monitor the BananaWiki SQLite DB directory for the "
                    "kinds of brief I/O stalls / permission glitches that "
                    "surface as `disk I/O error` to SQLite users.",
    )
    parser.add_argument(
        "--db-dir", required=False, default=None,
        help="Directory containing wiki.db (and its -wal / -shm "
             "sidecars).  Defaults to the BananaWiki config "
             "DATABASE_PATH if importable.",
    )
    parser.add_argument(
        "--db-file", required=False, default=None,
        help="Full path to the SQLite DB file.  Defaults to "
             "<db-dir>/wiki.db if --db-dir is provided.",
    )
    parser.add_argument(
        "--interval", type=float, default=5.0,
        help="Seconds between probes (default: 5).",
    )
    parser.add_argument(
        "--slow-threshold-ms", type=float, default=250.0,
        help="Flag fsync calls slower than this as 'slow' (default: 250).",
    )
    parser.add_argument(
        "--log", default=None,
        help="Optional path to a JSON-lines audit log "
             "(default: stdout only).",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Echo every event to stdout (default: only warnings / errors).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single sweep of every probe and exit "
             "(useful for cron / sanity-check).",
    )
    parser.add_argument(
        "--smart-every", type=int, default=12,
        help="Run SMART check every N intervals (default: 12 -- "
             "i.e. once per minute when interval=5).",
    )
    parser.add_argument(
        "--print-systemd-unit", action="store_true",
        help="Print a ready-to-install systemd unit file to stdout "
             "and exit.",
    )
    return parser.parse_args(argv)


def _resolve_db_paths(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve --db-dir / --db-file from args (or BananaWiki config)."""
    db_dir = args.db_dir
    db_file = args.db_file
    if not db_dir and not db_file:
        # Last resort: try to import the BananaWiki config so an
        # operator can just run the script with no arguments.
        try:
            import config  # type: ignore
            db_file = config.DATABASE_PATH
        except Exception:
            raise SystemExit(
                "io_health_check: pass --db-dir (and optionally "
                "--db-file), or run from the BananaWiki checkout so "
                "config.DATABASE_PATH is importable.",
            ) from None
    if db_dir and not db_file:
        # Convention: <db-dir>/wiki.db -- but pick whatever .db exists.
        candidate = os.path.join(db_dir, "wiki.db")
        if not os.path.exists(candidate):
            existing = [
                os.path.join(db_dir, f)
                for f in os.listdir(db_dir)
                if f.endswith(".db")
            ]
            if existing:
                candidate = existing[0]
        db_file = candidate
    if db_file and not db_dir:
        db_dir = os.path.dirname(os.path.abspath(db_file))
    return os.path.abspath(db_dir), os.path.abspath(db_file)


def main(argv: list[str] | None = None) -> int:
    """Entry point -- parse args, set up the logger, run the probe loop."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.print_systemd_unit:
        db_dir = args.db_dir or "/opt/BananaWiki/instances/wiki__apex"
        _emit_systemd_unit(db_dir)
        return 0

    db_dir, db_file = _resolve_db_paths(args)
    logger = _Logger(args.log, args.verbose)
    stats = _Stats()

    logger.summary(
        f"iocheck starting: db_dir={db_dir} db_file={db_file} "
        f"interval={args.interval}s slow>{args.slow_threshold_ms}ms",
    )

    # One-shot probes on startup.
    _run_integrity_check(db_file, logger)
    _read_new_dmesg(stats, logger)
    _read_smart_once(db_dir, logger)
    perms_ok = _check_permissions(db_dir, db_file, logger)
    if not perms_ok:
        stats.perm_errors += 1
        logger.summary(
            "PERMISSION PROBLEM detected -- see log; fix this first "
            "(common culprit: -wal / -shm owned by root after a backup).",
        )

    try:
        sweep = 0
        while True:
            sweep += 1
            elapsed_ms, errname = _probe_fsync_latency(
                db_dir, logger, args.slow_threshold_ms,
            )
            stats.samples += 1
            if errname:
                stats.io_errors += 1
            elif elapsed_ms is not None:
                stats.max_fsync_ms = max(stats.max_fsync_ms, elapsed_ms)
                if elapsed_ms >= args.slow_threshold_ms:
                    stats.slow_fsyncs += 1

            _read_new_dmesg(stats, logger)
            if args.smart_every > 0 and sweep % args.smart_every == 0:
                _read_smart_once(db_dir, logger)

            # Periodic one-liner summary -- keeps the operator informed
            # without flooding their terminal.
            if sweep % max(1, int(60 / max(args.interval, 0.5))) == 0:
                logger.summary(
                    f"sweep #{sweep}: samples={stats.samples} "
                    f"slow={stats.slow_fsyncs} io_errors={stats.io_errors} "
                    f"perm_errors={stats.perm_errors} "
                    f"max_fsync={stats.max_fsync_ms:.1f}ms",
                )

            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.summary("iocheck stopped (ctrl-c).")
    finally:
        logger.summary(
            f"iocheck final: samples={stats.samples} slow={stats.slow_fsyncs} "
            f"io_errors={stats.io_errors} perm_errors={stats.perm_errors} "
            f"max_fsync={stats.max_fsync_ms:.1f}ms",
        )
        logger.close()

    # Non-zero exit if we saw anything alarming, so the script can be
    # used in CI / a one-shot health check.
    if stats.io_errors or stats.perm_errors:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
