"""Syntax highlighting in helper processes that can be stopped (used by :mod:`bananawiki.wiki.markdown`).

Pygments lexers are regular expressions, and some of them, common ones
included (Bash, Scala, reStructuredText ...), need exponential time on a few
dozen crafted characters inside a single token. Python cannot interrupt a
regular expression, so no time check between tokens bounds them. Every web
process therefore lexes in at most :data:`HELPERS` helper processes of its
own: a highlighting that outlives the time it was given ends with its
helper, and the code stays plain. Where helpers cannot run, code stays plain.

The helpers are started (``spawn``) on first use and live as long as the
process; they import nothing but Pygments and leave when the process does.
"""

from __future__ import annotations

import logging
import multiprocessing
import pickle  # nosec B403 - only between this process and its own helpers
import threading
import time
from typing import Any

log = logging.getLogger("bananawiki.wiki")

HELPERS = 2  # per web process; more blocks at once wait for one (see WAIT_SECONDS)
START_SECONDS = 30.0  # a new helper has this long to import Pygments
WAIT_SECONDS = 2.0  # at most, and never past the time the code has left; then it stays plain


class _Helper:
    def __init__(self, context: Any) -> None:
        self.conn, child = context.Pipe()
        self.process = context.Process(target=_serve, args=(child,), daemon=True, name="bananawiki-highlighter")
        self.process.start()
        child.close()
        if not self.conn.poll(START_SECONDS) or self.conn.recv() != "ready":
            self.stop()
            raise RuntimeError("the highlighting helper did not start")

    def run(self, request: bytes, seconds: float) -> str | None:
        """The helper's answer to *request*; raises TimeoutError (and ends the helper) after *seconds*."""
        self.conn.send_bytes(request)
        if not self.conn.poll(max(seconds, 0.0)):
            self.stop()
            raise TimeoutError
        return self.conn.recv()

    def alive(self) -> bool:
        return self.process.is_alive()

    def stop(self) -> None:
        try:
            self.conn.close()
        finally:
            if self.process.is_alive():
                self.process.kill()
            self.process.join(5)


_lock = threading.Condition()
_idle: list[_Helper] = []
_running = 0
_off_until = 0.0  # after a helper failed to start, code stays plain until then (time.monotonic)
RETRY_SECONDS = 60.0


def _take(wait: float) -> _Helper | None:
    """A helper for this thread, started if needed; None when none is free within *wait* seconds or
    helpers cannot run."""
    global _running, _off_until
    deadline = time.monotonic() + wait
    with _lock:
        while True:
            if time.monotonic() < _off_until:
                return None
            while _idle:
                helper = _idle.pop()
                if helper.alive():
                    return helper
                _running -= 1
            if _running < HELPERS:
                _running += 1
                break
            left = deadline - time.monotonic()
            if left <= 0 or not _lock.wait(left):
                return None
    try:
        return _Helper(multiprocessing.get_context("spawn"))
    except Exception:  # noqa: BLE001 - frozen builds, sandboxes without processes: code stays plain
        log.exception("Syntax highlighting is off for a minute: its helper process could not start")
        with _lock:
            _running -= 1
            _off_until = time.monotonic() + RETRY_SECONDS
            _lock.notify_all()
        return None


def _give_back(helper: _Helper | None) -> None:
    global _running
    with _lock:
        if helper is not None and helper.alive():
            _idle.append(helper)
        else:
            _running -= 1
        _lock.notify()


def highlight(chunks: list[str], lexer: Any, formatter: Any,
              seconds: float) -> tuple[str | None, float, bool, bool]:
    """``(html, seconds spent, out of time, no helper)`` for *chunks* of code lexed one by one with *lexer*.

    Everything counts against *seconds*: waiting for a free helper, starting
    one, and the lexing. *html* is None when the code stays plain; *out of
    time* then says the caller has nothing left for later code either (the
    helper needed more than *seconds*, none was free in time, or none can
    run), and *no helper* that the code itself was not at fault (a result
    not worth keeping).
    """
    try:
        # The lexer goes as its class and options (some keep compiled rules that cannot be sent); its
        # filters come back from the options.
        request = pickle.dumps((chunks, type(lexer), lexer.options, formatter))
    except Exception:  # noqa: BLE001 - something that cannot be sent stays plain
        return None, 0.0, False, False
    start = time.monotonic()
    helper = _take(min(WAIT_SECONDS, seconds))
    if helper is None:
        return None, time.monotonic() - start, True, True
    try:
        html = helper.run(request, seconds - (time.monotonic() - start))
    except TimeoutError:
        helper = None
        return None, time.monotonic() - start, True, False
    except (OSError, EOFError):  # the helper died: start another next time
        helper.stop()
        helper = None
        return None, time.monotonic() - start, False, True
    finally:
        _give_back(helper)
    return html, time.monotonic() - start, False, False


def render(chunks: list[str], lexer: Any, formatter: Any) -> str:
    """What ``pygments.highlight`` makes of the code, each piece of *chunks* lexed on its own."""
    import io

    from pygments.filter import apply_filters

    def pieces():
        for chunk in chunks:
            for _index, token, value in lexer.get_tokens_unprocessed(chunk):
                yield token, value

    tokens = pieces()
    if lexer.filters:
        tokens = apply_filters(tokens, lexer.filters, lexer)
    out = io.StringIO()
    formatter.format(tokens, out)
    return out.getvalue()


def _serve(conn: Any) -> None:
    """The helper process: highlight what the parent sends until it goes away."""
    conn.send("ready")
    while True:
        try:
            request = conn.recv_bytes()
        except (EOFError, OSError):
            return
        try:
            chunks, lexer_class, options, formatter = pickle.loads(request)  # noqa: S301  # nosec B301 - from the parent
            html: str | None = render(chunks, lexer_class(**options), formatter)
        except Exception:  # noqa: BLE001 - a lexer bug leaves the code plain, never the page broken
            html = None
        conn.send(html)
