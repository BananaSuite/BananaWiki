"""Piper in a child process of the read-aloud worker.

``python -P piper_child.py`` reads a JSON request on standard input:
``model`` and ``config`` (the voice files), ``chunks`` (the text, see
:func:`.text.speech_chunks`), ``scales`` (length, noise, noise_w),
``output`` (the WAV file to write) and ``memory_mb``. It exits 0 once the
WAV is written; otherwise it prints one line on standard error and exits
with :data:`FAILED`, :data:`NOT_INSTALLED` or :data:`OUT_OF_MEMORY`.

On POSIX systems the worker runs Piper this way, so a page that exhausts
the memory or hangs the speech engine ends this process, never the worker:
the worker kills it after ``BW_TTS_MAX_JOB_SECONDS``, and with
``memory_mb`` an address-space limit (``RLIMIT_AS``) makes allocations fail
here before the server runs out of memory. ONNX Runtime reports such a
failure as an ordinary error (``std::bad_alloc``, ``BFCArena``); it counts as
:data:`OUT_OF_MEMORY` too, since the same page fails the same way again.
Under such a limit no sentence longer than :data:`MAX_SENTENCE_PHONEMES`
is read in one go.
The process leaves as soon as its worker is gone, so a killed worker leaves
no synthesis running.

Only the standard library is imported before Piper; :func:`write_wav` also
serves the worker's in-process path (Windows, the packaged desktop app).
"""

from __future__ import annotations

import errno
import io
import json
import logging
import os
import sys
import threading
import time
import wave
from collections.abc import Iterable
from typing import Any

FAILED = 1
NOT_INSTALLED = 2
OUT_OF_MEMORY = 3

# How a failed allocation reads once ONNX Runtime or the dynamic loader has turned it into an exception.
_ALLOCATION_FAILURES = ("std::bad_alloc", "BFCArena", "Failed to allocate memory",
                        "failed to map segment from shared object")


def out_of_memory(error: BaseException) -> bool:
    """Whether *error* means that Piper ran out of memory (or of address space)."""
    if isinstance(error, MemoryError) or (isinstance(error, OSError) and error.errno == errno.ENOMEM):
        return True
    return any(marker in str(error) for marker in _ALLOCATION_FAILURES)


def address_space_limited() -> bool:
    """Whether this process runs under an address-space limit (``RLIMIT_AS``), such as ``BW_MEMORY_LIMIT_MB``."""
    try:
        import resource
    except ImportError:  # not a POSIX system
        return False
    try:
        return resource.getrlimit(resource.RLIMIT_AS)[0] != resource.RLIM_INFINITY
    except (OSError, ValueError):
        return False


# Piper reads a sentence in one go, and its memory grows with the sentence's phonemes. Within 768 MB of
# address space a sentence of 300 phonemes failed on 2 CPUs without MALLOC_ARENA_MAX and
# OPENBLAS_NUM_THREADS, and one of 450 failed with them on 16. Sentences are as espeak-ng finds them: a full
# stop followed by a small letter ends none, so a list of commands (`ls -la`, `cd ~`) is one long sentence.
MAX_SENTENCE_PHONEMES = 250
# Where a part is cut, best first (the spoken text has single spaces, see text.speech_chunks).
_PART_BREAKS = (
    (". ", "! ", "? ", "… ", "。", "！", "？"),
    ("; ", ": ", ", ", "；", "：", "，", "、"),
    (" ",),
)


def _cut(text: str) -> int:
    """Where to cut *text* in two: at the break nearest its middle (a sentence end, else a clause mark, else
    a space) within its middle half, else at the middle."""
    low, middle, high = len(text) // 4, len(text) // 2, len(text) - len(text) // 4
    for marks in _PART_BREAKS:
        ends = []
        for mark in marks:
            index = text.find(mark, low, high)
            while index >= 0:
                ends.append(index + len(mark.rstrip()))
                index = text.find(mark, index + 1, high)
        ends = [end for end in ends if 0 < end < len(text)]
        if ends:
            return min(ends, key=lambda end: abs(end - middle))
    return max(1, middle)


def _short_parts(voice: Any, text: str, limit: int) -> list[str]:
    """*text* in parts in which Piper reads no sentence of more than *limit* phonemes.

    A part that is too long is cut in two and each half is checked again,
    so text that fits is never cut, and a sentence is only cut as often
    as it has to be. A single character is never cut.
    """
    pending, parts = [text], []
    while pending:
        part = pending.pop()
        if len(part) < 2 or max(map(len, voice.phonemize(part)), default=0) <= limit:
            parts.append(part)
            continue
        cut = _cut(part)
        pending.extend(piece for piece in (part[cut:].strip(), part[:cut].strip()) if piece)
    return parts


def _placeholder_format(wav: wave.Wave_write) -> None:
    """A format for a file Piper may leave without audio; Piper sets the real one first."""
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(22_050)


def write_wav(voice: Any, chunks: Iterable[str], syn_config: Any, output: str, max_phonemes: int = 0) -> int:
    """Synthesise *chunks* one after another into the WAV file *output*.

    Each piece goes through Piper's ``synthesize_wav`` on its own and its
    frames are appended, so only one piece is held in memory. With
    *max_phonemes*, a piece holding a longer sentence is cut further first
    (see :data:`MAX_SENTENCE_PHONEMES`). Returns the number of frames written.
    """
    frames = 0
    shape: tuple[int, int, int] | None = None
    with wave.open(output, "wb") as target:
        _placeholder_format(target)
        for chunk in chunks:
            for text in _short_parts(voice, chunk, max_phonemes) if max_phonemes else (chunk,):
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as part:
                    _placeholder_format(part)
                    voice.synthesize_wav(text, part, syn_config=syn_config)
                buffer.seek(0)
                with wave.open(buffer, "rb") as part:
                    count = part.getnframes()
                    if not count:
                        continue
                    current = (part.getnchannels(), part.getsampwidth(), part.getframerate())
                    if shape is None:
                        shape = current
                        target.setnchannels(current[0])
                        target.setsampwidth(current[1])
                        target.setframerate(current[2])
                    elif current != shape:
                        raise ValueError("the voice changed its audio format between two pieces")
                    target.writeframes(part.readframes(count))
                frames += count
    return frames


def _leave_with_parent() -> None:
    """Exit as soon as the worker that started this process is gone."""
    parent = os.getppid()

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(1)
        os._exit(FAILED)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def _limit_memory(megabytes: int) -> None:
    """Lower ``RLIMIT_AS`` to *megabytes* (never above a limit inherited from the worker)."""
    if megabytes <= 0:
        return
    try:
        import resource
    except ImportError:  # not a POSIX system
        return
    ceiling = megabytes * 1024 * 1024
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            ceiling = min(ceiling, hard)
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
    except (OSError, ValueError):
        pass  # a safety net only: synthesis still runs, bounded in time by the worker


def _fail(code: int, message: str) -> int:
    print(message.replace("\n", " ")[:500], file=sys.stderr, flush=True)
    return code


def main() -> int:
    request = json.load(sys.stdin)
    _leave_with_parent()
    _limit_memory(int(request.get("memory_mb") or 0))
    # Piper logs a warning per unknown phoneme; the worker bounds what it reads from stderr.
    logging.getLogger().addHandler(logging.NullHandler())
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
    try:
        from piper import PiperVoice, SynthesisConfig  # type: ignore[import-not-found]
    except (ImportError, MemoryError, OSError) as error:  # numpy starts its threads while it is imported
        if out_of_memory(error):
            return _fail(OUT_OF_MEMORY, "Piper ran out of memory.")
        if not isinstance(error, ImportError):
            raise
        return _fail(NOT_INSTALLED, "Piper is not installed on the server.")
    try:
        length, noise, noise_w = request["scales"]
        voice = PiperVoice.load(request["model"], config_path=request["config"])
        syn_config = SynthesisConfig(length_scale=length, noise_scale=noise, noise_w_scale=noise_w)
        frames = write_wav(voice, request["chunks"], syn_config, request["output"],
                           MAX_SENTENCE_PHONEMES if address_space_limited() else 0)
    except Exception as error:  # noqa: BLE001 - reported to the worker, which fails the job
        if out_of_memory(error):
            return _fail(OUT_OF_MEMORY, "Piper ran out of memory.")
        return _fail(FAILED, f"Piper failed: {error}")
    if not frames:
        return _fail(FAILED, "Piper produced an empty file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
