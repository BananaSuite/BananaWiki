#!/usr/bin/env python3
"""Run the BananaWiki durable TTS worker.

Web processes only enqueue rows in ``tts_generations``.  This command should
run as a separate long-lived process to synthesize those rows with the local
Piper backend.

When the host has switched TTS off for this wiki (``BW_MANAGED_TTS_DISABLED``
or ``BW_EASY_WIKI``) the worker starts but stays idle and never claims a row.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import db  # noqa: E402
from helpers._tts import (  # noqa: E402
    TTS_WORKER_DEFAULT_POLL_INTERVAL_SECONDS,
    run_tts_worker_loop,
    tts_generation_disabled_by_host,
)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="process currently queued work once, then exit",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="exit after processing this many jobs",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=TTS_WORKER_DEFAULT_POLL_INTERVAL_SECONDS,
        help="seconds to sleep when no pending TTS work exists",
    )
    parser.add_argument(
        "--tts-folder",
        default=getattr(config, "TTS_FOLDER", None),
        help="cached audio folder; defaults to BW_TTS_FOLDER/config.TTS_FOLDER",
    )
    parser.add_argument(
        "--skip-recovery",
        action="store_true",
        help="do not reset orphaned processing rows at startup",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("BW_TTS_WORKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db.init_db()
    tts_folder = args.tts_folder or os.path.join(ROOT, "instance", "tts")
    logging.getLogger("bananawiki.tts").info(
        "Starting TTS worker: folder=%s once=%s max_jobs=%s",
        tts_folder, args.once, args.max_jobs,
    )
    if tts_generation_disabled_by_host():
        # Stay running rather than exit: the hosting supervisors restart a
        # worker that exits, and an idle process is cheaper than that loop.
        logging.getLogger("bananawiki.tts").info(
            "TTS generation is switched off for this wiki by the host; "
            "the worker stays idle and processes no rows",
        )
    processed = run_tts_worker_loop(
        tts_folder,
        poll_interval=args.poll_interval,
        once=args.once,
        max_jobs=args.max_jobs,
        recover=not args.skip_recovery,
    )
    if args.once or args.max_jobs is not None:
        logging.getLogger("bananawiki.tts").info(
            "TTS worker exiting after processing %s job(s)", processed,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
