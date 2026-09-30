#!/usr/bin/env python3
"""The read-aloud worker (the ``<name>-tts`` systemd unit runs ``python scripts/tts_worker.py``).

Runs :func:`bananawiki.wiki.features.tts.worker.main` from the release root.
The worker idles instead of exiting while the feature is off, the host
disabled generation or no voice engine is installed, so systemd never sees
a crash loop. Flags: ``--once``, ``--max-jobs N``, ``--poll-interval S``,
``--threads N``, ``--skip-recovery`` (``--tts-folder`` from 1.4 is accepted
and ignored: the folder is ``BW_TTS_FOLDER``).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bananawiki.wiki.features.tts.worker import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
