"""The ``<service>-tts`` process (``scripts/tts_worker.py`` delegates here).

Runs ``bananawiki.wiki.features.tts.worker.main(argv)`` when the text-to-speech
feature ships a worker. Otherwise (or when its optional dependencies are
missing) it stays idle instead of exiting, because systemd and the tenant
supervisor restart a worker that exits and a crash loop helps nobody.
The 1.4 flags (``--once``, ``--max-jobs``, ``--poll-interval``,
``--tts-folder``, ``--skip-recovery``) are passed through unchanged.
"""

from __future__ import annotations

import importlib
import logging
import signal
import sys
import threading
from collections.abc import Callable, Sequence

log = logging.getLogger("bananawiki.tts")


def _worker() -> Callable[[list[str]], int] | None:
    try:
        module = importlib.import_module("bananawiki.wiki.features.tts.worker")
    except ImportError as error:
        log.warning("No text-to-speech worker is available (%s); staying idle.", error)
        return None
    entry = getattr(module, "main", None)
    return entry if callable(entry) else None


def idle(stop: threading.Event) -> int:
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(3600):
        pass
    return 0


def main(argv: Sequence[str] | None = None, *, stop: threading.Event | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    arguments = list(sys.argv[1:] if argv is None else argv)
    entry = _worker()
    if entry is None:
        if "--once" in arguments or "--max-jobs" in arguments:
            return 0
        return idle(stop or threading.Event())
    return int(entry(arguments) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
