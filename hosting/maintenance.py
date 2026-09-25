"""Dedicated hosting maintenance process.

Runs lifecycle cleanup outside Gunicorn so worker reloads cannot duplicate
or silently stop retention-critical jobs.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading

from .app import run_maintenance_once
from .db import init_hosting_db

logger = logging.getLogger("hosting.maintenance")
_stop = threading.Event()


def _request_stop(_signum, _frame):
    _stop.set()


def run(*, once: bool = False, interval: int = 300) -> int:
    init_hosting_db()
    if not once:
        try:
            from .instance_manager import recover_running_instances
            recover_running_instances()
        except Exception:
            logger.exception("Hosted-instance recovery failed")
    if _stop.is_set():
        return 0
    try:
        from .gdrive_backup import start_backup_scheduler
        start_backup_scheduler()
    except Exception:
        logger.warning("Could not start Google Drive backup scheduler", exc_info=True)
    while True:
        if _stop.is_set():
            return 0
        run_maintenance_once()
        if once or _stop.wait(max(30, int(interval))):
            return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BananaWiki hosting maintenance")
    parser.add_argument("--once", action="store_true", help="Run one pass and exit")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between passes")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    logging.basicConfig(level=logging.INFO)
    return run(once=args.once, interval=args.interval)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
