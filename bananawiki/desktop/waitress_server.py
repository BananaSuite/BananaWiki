"""The launcher's waitress server, with the limits a proxy would otherwise set.

The desktop wiki has no proxy in front of it (a standalone wiki has Caddy),
so the server itself bounds what one client can hold:

* **Request headers** must arrive within *header_timeout* seconds, counted
  from when the connection starts waiting for a request (accepted, or done
  with the previous response). Waitress's own ``channel_timeout`` counts from
  the last byte received, so a client that sends one header byte now and
  then would keep its connection forever.
* **One address** keeps at most *per_address* connections open; a further one
  is closed straight away. Waitress otherwise stops accepting anything once
  ``connection_limit`` connections are open, so one device could shut out
  every other, the teacher's own browser included.
* **A response** whose client takes in nothing for *write_timeout* seconds is
  dropped, as the gunicorn child does (``BW_WRITE_TIMEOUT``,
  :mod:`bananawiki.ops.write_timeout`). A body larger than waitress's output
  buffer (``outbuf_high_watermark``) otherwise keeps the application thread
  waiting for room with no timeout, and waitress never closes a channel with
  a request in progress, so the response, its thread and its download place
  (:mod:`bananawiki.core.downloads`) stay taken until the wiki stops.
* **Request bodies** are bounded before they are read. Waitress takes in a
  whole body, spooling it to a temporary file past ``inbuf_overflow``, before
  the application can refuse it: ``max_request_body_size`` (the launcher sets
  it to the wiki's largest upload) answers 413 right after the headers, and
  one address may have at most *body_budget* bytes of bodies arriving or
  waiting to be served at once (Content-Length counted when the headers end, a
  chunked body as it arrives). Otherwise sixteen connections of one device
  could each spool 1 GiB to the system's temporary folder. A refused request
  that sent ``Expect: 100-continue`` gets no ``100 Continue``: waitress would
  send one and then take in the body anyway.

Expired channels are found by the server's maintenance pass (every
``cleanup_interval`` seconds) and closed on the server thread, between two
rounds of the event loop: closing one while the loop builds its list of
sockets would hand ``select`` a closed descriptor. Closing wakes an
application thread waiting for room, which then sees the client gone, ends
the response and closes its body (giving back its download place).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from waitress.adjustments import Adjustments
from waitress.channel import HTTPChannel
from waitress.parser import HTTPRequestParser
from waitress.server import TcpWSGIServer
from waitress.task import ThreadedTaskDispatcher
from waitress.utilities import RequestEntityTooLarge

log = logging.getLogger("bananawiki.desktop")


class _Parser(HTTPRequestParser):
    """A request that charges its body to its address's budget (see :meth:`LimitedServer.charge`)."""

    def __init__(self, adj: Any, channel: _Channel):
        super().__init__(adj)
        self._channel = channel
        self.charged = 0

    def received(self, data: bytes) -> int:
        consumed = super().received(data)
        if self.error is None and self.headers_finished and not self.completed:
            # Content-Length is known once the headers end; a chunked body is charged as it arrives.
            wanted = self.content_length if self.content_length > 0 else self.body_bytes_received
            if wanted > self.charged and not self._channel.server.charge(self, wanted):
                self.error = RequestEntityTooLarge("too many request bodies arriving from one address")
                self.completed = True
        if self.error is not None:
            # The channel sends "100 Continue" to a request that expects it once its headers end, error or not, and
            # that marks it incomplete again: its body would then be spooled (uncharged) before the answer.
            self.expect_continue = False
        return consumed

    def release(self) -> None:
        self._channel.server.charge(self, 0)

    def close(self) -> None:
        self.release()
        super().close()


class _Channel(HTTPChannel):
    """A connection that knows since when it waits for request headers."""

    _counted = False

    def parser_class(self, adj: Any) -> _Parser:  # waitress calls self.parser_class(self.adj)
        return _Parser(adj, self)

    def __init__(self, server: Any, sock: Any, addr: Any, adj: Any, map: Any = None):
        try:
            super().__init__(server, sock, addr, adj, map=map)
        except BaseException:
            server.release(addr)
            raise
        self.waiting_since = self.creation_time
        self._counted = True  # the server counted it in accept()

    def service(self) -> None:
        try:
            super().service()
        finally:
            # The next request's headers are waited for from now (or once the response is sent, see expired()).
            self.waiting_since = time.time()

    def expired(self, now: float) -> str | None:
        """Why this channel should be closed now, if it should. Called on the server thread."""
        server = self.server
        pending = self.total_outbufs_len
        if pending:
            # The main loop sends whatever is pending while no request runs, and a running one's output once there
            # is at least send_bytes of it: no byte sent for that long means the client takes nothing in.
            sending = not self.requests or pending >= self.adj.send_bytes
            if server.write_timeout and sending and self.last_activity < now - server.write_timeout:
                return "stopped reading its response"
            if not self.requests:
                self.waiting_since = now  # still sending the previous response: no request is awaited yet
            return None
        if self.requests or (self.request is not None and self.request.headers_finished):
            return None  # a request is in progress, or its body is arriving
        if self.waiting_since < now - server.header_timeout:
            return "sent no complete request headers in time"
        return None

    def del_channel(self, map: Any = None) -> None:
        super().del_channel(map)
        # Bodies still arriving or queued are not closed by waitress on a close: give their budget back.
        for request in [self.request, *self.requests]:
            if isinstance(request, _Parser):
                request.release()
        if self._counted:
            self._counted = False
            self.server.release(self.addr)


class LimitedServer(TcpWSGIServer):
    """A waitress TCP server applying the limits of the module docstring."""

    channel_class = _Channel

    def __init__(self, *args: Any, header_timeout: float, write_timeout: float, per_address: int,
                 body_budget: int, **kwargs: Any):
        self.header_timeout = header_timeout
        self.write_timeout = write_timeout
        self.per_address = per_address
        self.body_budget = body_budget
        self._open: dict[str, int] = {}
        self._bodies: dict[str, int] = {}
        self._over_budget: set[str] = set()
        self._refusing: set[str] = set()
        self._lock = threading.Lock()
        super().__init__(*args, **kwargs)

    @staticmethod
    def _host(addr: Any) -> str:
        return str(addr[0] if isinstance(addr, tuple) else addr)

    def accept(self) -> Any:
        pair = super().accept()
        if pair is None:
            return None
        conn, addr = pair
        host = self._host(addr)
        with self._lock:
            if self._open.get(host, 0) >= self.per_address:
                refuse = True
                first = host not in self._refusing
                self._refusing.add(host)
            else:
                refuse = first = False
                self._open[host] = self._open.get(host, 0) + 1
        if refuse:
            if first:
                log.warning("%s has %d connections open: further ones are closed", host, self.per_address)
            conn.close()
            return None  # handle_accept() then makes no channel
        return pair

    def release(self, addr: Any) -> None:
        host = self._host(addr)
        with self._lock:
            left = self._open.get(host, 0) - 1
            if left > 0:
                self._open[host] = left
            else:
                self._open.pop(host, None)
            if left < self.per_address:
                self._refusing.discard(host)

    def charge(self, request: _Parser, size: int) -> bool:
        """Count *size* body bytes for *request* against its address (0: give them back).

        False, with nothing more counted, when that would take the address past its budget.
        """
        host = self._host(request._channel.addr)
        with self._lock:
            held = self._bodies.get(host, 0) - request.charged + size
            refused = size > request.charged and held > self.body_budget
            first = refused and host not in self._over_budget
            if refused:
                self._over_budget.add(host)
            else:
                request.charged = size
                if held > 0:
                    self._bodies[host] = held
                else:
                    self._bodies.pop(host, None)
                if held < self.body_budget // 2:
                    self._over_budget.discard(host)
        if first:
            log.warning("%s sends more than %d bytes of request bodies at once: further ones are refused",
                        host, self.body_budget)
        return not refused

    def body_bytes(self, host: str) -> int:
        with self._lock:
            return self._bodies.get(host, 0)

    def open_connections(self, host: str) -> int:
        with self._lock:
            return self._open.get(host, 0)

    def maintenance(self, now: float) -> None:
        super().maintenance(now)
        expired = [(channel, reason) for channel in list(self.active_channels.values())
                   if (reason := channel.expired(now)) is not None]
        if expired:
            self.trigger.pull_trigger(lambda: self._close(expired))

    def _close(self, expired: list[tuple[_Channel, str]]) -> None:
        now = time.time()
        for channel, reason in expired:
            # Still open and still expired: a request may have completed in between.
            if channel.socket is not None and channel.expired(now) is not None:
                log.info("Closing the connection of %s: it %s", self._host(channel.addr), reason)
                channel.handle_close()


def create(app: Any, sock: Any, *, header_timeout: float, write_timeout: float, per_address: int,
           body_budget: int, **adjustments: Any) -> LimitedServer:
    """A :class:`LimitedServer` serving *app* on the bound TCP socket *sock* (``waitress.create_server``'s work)."""
    adj = Adjustments(sockets=[sock], **adjustments)
    dispatcher = ThreadedTaskDispatcher()
    dispatcher.set_thread_count(adj.threads)
    return LimitedServer(app, {}, True, sock, dispatcher=dispatcher, adj=adj, bind_socket=False,
                         sockinfo=(sock.family, sock.type, sock.proto, sock.getsockname()),
                         header_timeout=header_timeout, write_timeout=write_timeout, per_address=per_address,
                         body_budget=body_budget)
