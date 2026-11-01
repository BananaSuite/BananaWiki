"""A deadline for request headers in the launcher's gunicorn child, and a cap per address.

Gunicorn's gthread worker hands every new connection to a request thread,
which waits up to 5 s for a first byte and then reads the request line and
headers on a blocking socket with no timeout. A device on the network that
sends one header byte now and then therefore holds a thread for as long as
it likes, and a dozen such connections hold every thread of the child. No
setting or hook of gunicorn runs before the headers are read (``pre_request``
comes after them), so :class:`HeaderGuard` wraps the worker's ``handle``
(one connection, from accepting it) and ``handle_request`` (called once its
headers are parsed):

* a connection whose headers are not complete *header_timeout* seconds after
  its thread took it is shut down from a watchdog thread, which ends the
  blocked read; gunicorn then closes it as a client that went away;
* while *per_address* connections from one address are still sending their
  headers, a further one from that address is closed straight away. Browsers
  finish theirs at once, so only slow clients are ever counted.

A request body is read by the application after ``handle_request`` started,
with no deadline of the guard's: one address holds at most the threads its
requests keep busy, as with any client. Standard library only.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("bananawiki.desktop")


class HeaderGuard:
    """Holds the connections of a gunicorn worker that are still sending their request headers."""

    def __init__(self, header_timeout: float, per_address: int, clock: Callable[[], float] = time.monotonic):
        self.header_timeout = header_timeout
        self.per_address = per_address
        self._clock = clock
        self._lock = threading.Lock()
        self._reading: dict[int, tuple[Any, float]] = {}  # id(conn): (conn, deadline)
        self._open: dict[str, int] = {}
        self._refusing: set[str] = set()

    @staticmethod
    def _host(conn: Any) -> str:
        client = conn.client
        return str(client[0] if isinstance(client, tuple) else client)

    def _begin(self, conn: Any) -> bool | None:
        """Count *conn* as sending its headers: None when it may, else whether its address was just refused."""
        host = self._host(conn)
        with self._lock:
            if self._open.get(host, 0) >= self.per_address:
                first = host not in self._refusing
                self._refusing.add(host)
                return first
            self._open[host] = self._open.get(host, 0) + 1
            self._reading[id(conn)] = (conn, self._clock() + self.header_timeout)
        return None

    def _drop(self, conn: Any) -> bool:
        """Stop counting *conn* (with the lock held); False when it no longer was."""
        if self._reading.pop(id(conn), None) is None:
            return False
        host = self._host(conn)
        left = self._open.get(host, 0) - 1
        if left > 0:
            self._open[host] = left
        else:
            self._open.pop(host, None)
        if left < self.per_address:
            self._refusing.discard(host)
        return True

    def _end(self, conn: Any) -> None:
        with self._lock:
            self._drop(conn)

    def reading(self, host: str) -> int:
        """Connections from *host* still sending their headers."""
        with self._lock:
            return self._open.get(host, 0)

    def sweep(self) -> None:
        """Shut down the connections past their deadline (the thread reading them sees the client gone)."""
        now = self._clock()
        with self._lock:
            # Taken off the list here, so each is shut down once; handle()'s _end() then finds nothing.
            expired = [conn for conn, deadline in list(self._reading.values()) if deadline <= now and self._drop(conn)]
        for conn in expired:
            log.info("Closing the connection of %s: it sent no complete request headers in time", self._host(conn))
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already closed

    def install(self, worker: Any) -> None:
        """Wrap *worker*'s ``handle`` and ``handle_request`` and start the watchdog."""
        handle, handle_request = worker.handle, worker.handle_request

        def guarded_handle(conn: Any) -> Any:
            refused = self._begin(conn)
            if refused is not None:
                if refused:
                    log.warning("%s has %d connections sending request headers: further ones are closed",
                                self._host(conn), self.per_address)
                return False  # gunicorn closes the connection
            try:
                return handle(conn)
            finally:
                self._end(conn)

        def guarded_request(req: Any, conn: Any) -> Any:
            self._end(conn)  # the headers are in
            return handle_request(req, conn)

        worker.handle, worker.handle_request = guarded_handle, guarded_request
        interval = max(0.05, min(1.0, self.header_timeout / 4))

        def watch() -> None:
            while True:
                time.sleep(interval)
                try:
                    self.sweep()
                except Exception:  # noqa: BLE001 - the watchdog must keep running
                    log.exception("The request header watchdog failed")

        threading.Thread(target=watch, name="bananawiki-header-deadline", daemon=True).start()
