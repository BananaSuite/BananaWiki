# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""A stall timeout for the responses Gunicorn writes (R-27).

Gunicorn has no write timeout: a gthread worker writes a response with
blocking ``sendall`` and ``sendfile`` calls, so a client that stops reading a
long download (the personal data export, a whole-site export, an attachment)
holds a worker thread for as long as it keeps the connection open. Caddy
2.11.6 and newer drop such a client after a minute (``write_idle``); older
Caddy releases and other proxies wait for it.

:func:`guard` sets a timeout on the client connection once the application
has returned its response, so request bodies are read as before. A write
that has to wait that long for room in the connection then fails, Gunicorn
closes the connection and the thread is free again.

A reader is cut off when it frees too little room within one period, and
room comes in steps. ``sendall`` applies the timeout to the whole call, so
the body is handed to Gunicorn at most :data:`MAX_WRITE` bytes at a time.
``sendfile`` (``wsgi.file_wrapper``, left untouched so files stay zero-copy)
fills the whole kernel send buffer, and the kernel reports a TCP socket
writable again only once a third of it has drained; so on TCP connections the
bytes queued but not yet sent are kept near :data:`UNSENT`
(``TCP_NOTSENT_LOWAT``), which does not limit the data in flight. A
connection read slowly from the start then needs :data:`FLOOR` bytes per
period. The kernel grows a receive buffer while data arrives fast (up to the
``net.ipv4.tcp_rmem`` maximum); once full, it takes more data only after
about a sixteenth of it is free again, and the sender cannot see the reads
before that. A client that slowed down after a fast start, or a proxy
connection that carried fast responses, therefore needs up to
:data:`GROWN_FLOOR`. The timeout is the only lever for slower readers.

Generated bodies are corked (``TCP_CORK``, Linux) until their last piece is
written. Gunicorn sets ``TCP_NODELAY``, so every write that does not end on a
segment boundary would leave a small segment; a receive buffer at its maximum
runs out of memory on those before its window is used, drops data, and the
retransmissions then wait on the reader too.

HTTP/2 streams share their connection, so they are left alone (the shipped
configurations serve HTTP/1.1). Meant for thread workers: async workers
recognise an "already handled" response by identity, which the wrapper hides.

Standard library only (see :mod:`bananawiki.ops`).
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable, Iterator
from typing import Any

# Caddy's write_max_chunk.
MAX_WRITE = 64 * 1024
UNSENT = 2 * MAX_WRITE
# Bytes per timeout period that kept a reader connected, twice what was measured
# on Linux 6.18 over loopback: about 128 KiB read slowly from the start, 2 MiB
# with a 32 MiB receive buffer (the maximum on recent kernels, 6 MiB on older
# ones). At the default 300 s, about 1 and 14 KiB/s.
FLOOR = 256 * 1024
GROWN_FLOOR = 4 * 1024 * 1024
# Only HTTP/2 streams have it; no request header maps to it (unlike HTTP_VERSION).
_HTTP2 = "gunicorn.http2.send_trailers"

WSGIApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]


def _set(sock: Any, name: str, value: int) -> bool:
    """A TCP option on a TCP connection, where the platform has it."""
    option = getattr(socket, name, None)
    if option is None or getattr(sock, "family", None) not in (socket.AF_INET, socket.AF_INET6):
        return False
    try:
        sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except OSError:
        return False
    return True


class _Pieces:
    """The body in writes of at most :data:`MAX_WRITE` bytes, uncorking *sock* after the last one."""

    def __init__(self, body: Iterable[bytes], sock: Any = None) -> None:
        self._body = body
        self._corked = sock

    def __iter__(self) -> Iterator[bytes]:
        for item in self._body:
            if len(item) <= MAX_WRITE:
                yield item
            else:
                for start in range(0, len(item), MAX_WRITE):
                    yield item[start:start + MAX_WRITE]
        self._uncork()  # what is left goes out now, before the access log is written

    def _uncork(self) -> None:
        sock, self._corked = self._corked, None
        if sock is not None:
            _set(sock, "TCP_CORK", 0)

    def close(self) -> None:
        self._uncork()
        close = getattr(self._body, "close", None)
        if close is not None:
            close()


def guard(app: WSGIApp, timeout: float) -> WSGIApp:
    """*app*, giving up on a client that takes in too little for *timeout* seconds (0: wait forever)."""
    if timeout <= 0:
        return app

    def guarded(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        body = app(environ, start_response)
        sock = environ.get("gunicorn.socket")
        if sock is None or _HTTP2 in environ:
            return body
        try:
            sock.settimeout(timeout)
        except OSError:
            return body
        _set(sock, "TCP_NOTSENT_LOWAT", UNSENT)  # else sendfile needs a third of the send buffer per period
        wrapper = environ.get("wsgi.file_wrapper")
        if isinstance(wrapper, type) and isinstance(body, wrapper):
            return body  # Gunicorn sends it with sendfile only while it is the file wrapper itself
        return _Pieces(body, sock if _set(sock, "TCP_CORK", 1) else None)

    return guarded
