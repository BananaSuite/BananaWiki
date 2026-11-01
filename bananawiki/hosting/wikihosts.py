"""Requests for a wiki's hostname that reach the portal instead of the wiki.

Caddy normally sends each wiki host straight to its container (the runtime
agent writes those routes). A wiki host still ends up here when that route is
missing: the Caddyfile was written by hand without the routes import, the
server still uses the 1.4 proxy configuration that sends every host to the
portal, the agent cannot publish routes, or the container has only just
(re)started. Without this middleware the portal would answer such requests as
its own, and opening a wiki would show the portal's sign-in page or dashboard.

For a wiki host this middleware never lets the portal application run:

* a running, healthy wiki is proxied, so it works even without per-wiki
  routes;
* anything else gets a small status page on the wiki's own address: no wiki
  here, paused, suspended, expired, or starting (refreshes by itself).

Hosts that are not wiki hosts (the portal and base domains, addresses,
``localhost``, reserved labels such as ``www``) go to the portal unchanged.
"""

from __future__ import annotations

import http.client
import logging
import os
import re
import secrets
import socket
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Iterator
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from flask import Flask, render_template

from ..core import web
from ..core.timeutil import is_past, now_sql
from .runtime import RuntimeFailure

log = logging.getLogger("bananawiki.hosting.wikihosts")

CONNECT_TIMEOUT = 5
# Below the portal's worker timeout (HOSTING_WORKER_TIMEOUT, 120 s), so a slow
# wiki gets a clean status page instead of a killed worker.
READ_TIMEOUT = 100
# A wiki that trickles its response would otherwise keep its slot for as long
# as it likes; past this the response is cut off.
RESPONSE_SECONDS = 300
CHUNK = 64 * 1024
# Portal workers have few threads (HOSTING_THREADS, default 4): proxied wiki
# traffic never takes the last one, and one busy wiki always leaves one of
# the others to the remaining wikis.
PER_WIKI = 3
QUEUE_SECONDS = 10
# Requests that may wait for a proxy slot at once, per process and per wiki. The
# portal's Gunicorn settings run a thread for each waiting place on top of
# HOSTING_THREADS, so waiting requests never take the threads the portal's own
# pages use, and one that finds no waiting place is answered "busy" at once.
# Proxied requests do run on HOSTING_THREADS: all but one of them at most
# (_Slots), and for one wiki one fewer still, up to PER_WIKI (2 by default).
WAITING_PLACES = 4
WAITING_PER_WIKI = 3
# Wiki-host requests per client network and minute, per process. Twice the
# wikis' own limit, which does not count their static files.
RATE_LIMIT = 600
# How often a portal worker checks whether Gunicorn is retiring it (max_requests, a restart): see watch().
DRAIN_CHECK_SECONDS = 0.25
MAX_BUFFERED_BODY = 16 * 1024 * 1024
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
    "trailer", "transfer-encoding", "upgrade",
})
STRIPPED_REQUEST = HOP_BY_HOP | {"host", "forwarded", "x-forwarded-for", "x-forwarded-proto",
                                 "x-forwarded-host", "x-forwarded-port", "x-forwarded-prefix", "x-real-ip"}
PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")


def request_host(environ: dict[str, Any]) -> str:
    """The Host of the request, lower case, without port or trailing dot."""
    host = (environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0].rstrip(".") if ":" in host else host.rstrip(".")


def _is_address(host: str) -> bool:
    return host.startswith("[") or host.replace(".", "").isdigit()


def _thread_count() -> int:
    """HOSTING_THREADS as hosting/gunicorn.conf.py reads it (1 to 32)."""
    try:
        return max(1, min(32, int(os.environ.get("HOSTING_THREADS") or 4)))
    except ValueError:
        return 4


class _Slot:
    """One proxied request: a place for its wiki and one in the process-wide pool."""

    def __init__(self, wiki: threading.BoundedSemaphore, waiting_here: threading.BoundedSemaphore,
                 pool: threading.BoundedSemaphore, waiting: threading.BoundedSemaphore, draining: threading.Event):
        self._wiki = wiki
        self._waiting_here = waiting_here
        self._pool = pool
        self._waiting = waiting
        self._draining = draining

    def acquire(self, timeout: float) -> bool:
        """Take the slot, waiting up to *timeout* only in a waiting place of its wiki and of the process.

        Never while the process drains (:meth:`_Slots.drain`), and a wait in progress ends then.
        """
        if self._draining.is_set():
            return False
        if self._take():
            return True
        if not self._waiting_here.acquire(blocking=False):
            return False
        try:
            if not self._waiting.acquire(blocking=False):
                return False
            try:
                deadline = time.monotonic() + timeout
                if not self._wait(self._wiki, deadline):
                    return False
                if not self._wait(self._pool, deadline):
                    self._wiki.release()
                    return False
                return True
            finally:
                self._waiting.release()
        finally:
            self._waiting_here.release()

    def _wait(self, semaphore: threading.BoundedSemaphore, deadline: float) -> bool:
        while not self._draining.is_set():
            remaining = deadline - time.monotonic()
            if semaphore.acquire(timeout=max(0.0, min(DRAIN_CHECK_SECONDS, remaining))):
                return True
            if remaining <= 0:
                return False
        return False

    def _take(self) -> bool:
        if not self._wiki.acquire(blocking=False):
            return False
        if self._pool.acquire(blocking=False):
            return True
        self._wiki.release()
        return False

    def release(self) -> None:
        self._pool.release()
        self._wiki.release()


class _Slots:
    """Bounded concurrent proxied requests, per wiki and per portal process.

    Once the process drains (Gunicorn retires the worker), proxied requests
    are cut off and waiting ones answered at once: Gunicorn stops accepting
    connections and waits for the requests in progress before it starts a
    new worker, so a slow wiki would otherwise keep the portal from
    answering for as long as it likes.
    """

    def __init__(self, total: int | None = None, waiting: int | None = None) -> None:
        total = total or max(1, _thread_count() - 1)
        waiting = WAITING_PLACES if waiting is None else waiting
        self._per_wiki = max(1, min(PER_WIKI, total - 1))
        self._waiting_per_wiki = max(1, min(WAITING_PER_WIKI, waiting - 1))
        self._pool = threading.BoundedSemaphore(total)
        self._waiting = threading.BoundedSemaphore(max(1, waiting))
        self._lock = threading.Lock()
        self._slots: dict[str, tuple[threading.BoundedSemaphore, threading.BoundedSemaphore]] = {}
        self.draining = threading.Event()
        # The sockets themselves: the connection lets go of its socket once a response will close it.
        self._sockets: weakref.WeakSet[socket.socket] = weakref.WeakSet()
        _ALL_SLOTS.add(self)

    def get(self, name: str) -> _Slot:
        with self._lock:
            wiki = self._slots.get(name)
            if wiki is None:
                wiki = self._slots[name] = (threading.BoundedSemaphore(self._per_wiki),
                                            threading.BoundedSemaphore(self._waiting_per_wiki))
            return _Slot(wiki[0], wiki[1], self._pool, self._waiting, self.draining)

    def connect(self, address: str, port: int) -> http.client.HTTPConnection:
        """A connection to a wiki, which :meth:`drain` cuts off."""
        connection = http.client.HTTPConnection(address, port, timeout=CONNECT_TIMEOUT)
        connection.connect()
        with self._lock:
            self._sockets.add(connection.sock)
        if self.draining.is_set():  # drain() listed the sockets before this one was added
            connection.close()
            raise ConnectionAbortedError("the portal worker is shutting down")
        return connection

    def drain(self) -> None:
        """Stop proxying: no new slots, waits end, and every connection to a wiki is shut down."""
        self.draining.set()
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


_ALL_SLOTS: weakref.WeakSet[_Slots] = weakref.WeakSet()


def drain() -> None:
    """Drain the proxy slots of every portal application in this process (see :class:`_Slots`)."""
    for slots in list(_ALL_SLOTS):
        slots.drain()


def watch(alive: Callable[[], bool]) -> None:
    """Drain this process once *alive* turns false (hosting/gunicorn.conf.py passes the worker's ``alive``).

    Gunicorn has no hook for a worker that reached ``max_requests`` or got
    SIGTERM: it only stops its loop, so a thread checks.
    """

    def run() -> None:
        while alive():
            time.sleep(DRAIN_CHECK_SECONDS)
        drain()

    threading.Thread(target=run, name="wikihosts-drain", daemon=True).start()


class _Body:
    """The upstream response body; closing it frees the connection and the slot."""

    def __init__(self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection,
                 slot: _Slot, draining: threading.Event | None = None):
        self._response = response
        self._connection = connection
        self._slot: _Slot | None = slot
        self._draining = draining or threading.Event()
        self._deadline = time.monotonic() + RESPONSE_SECONDS

    def __iter__(self) -> Iterator[bytes]:
        while True:
            if self._draining.is_set():
                log.warning("Wiki response cut off: the portal worker is shutting down")
                return
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                log.warning("Wiki response cut off after %d seconds", RESPONSE_SECONDS)
                return
            try:
                if self._connection.sock is not None:
                    self._connection.sock.settimeout(min(READ_TIMEOUT, remaining))
                chunk = self._response.read1(CHUNK)
            except (OSError, http.client.HTTPException) as error:
                log.warning("Wiki response interrupted: %s", error)
                return
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            if self._slot is not None:
                self._slot.release()
                self._slot = None


class WikiHosts:
    """WSGI middleware in front of the portal application (inside ProxyFix)."""

    def __init__(self, app: Flask, wsgi_app: Any):
        self.app = app
        self.wsgi_app = wsgi_app
        self.slots = _Slots()

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
        host = request_host(environ)
        cfg = self.app.config["HOSTING"]
        if (cfg.hosting_mode != "subdomain" or not cfg.base_domain or not host or _is_address(host)
                or host in (cfg.base_domain, cfg.effective_portal_domain) or "." not in host):
            return self.wsgi_app(environ, start_response)
        with self.app.request_context(environ):
            # The portal's request limits never see a wiki host: count it here, before any lookup.
            decision = self._decide(host) if self._admit() else ("limited", host)
            page = None if decision is None or decision[0] == "proxy" else self._page(*decision)
        if decision is None:
            return self.wsgi_app(environ, start_response)
        if page is not None:
            status, headers, body = page
            start_response(status, headers)
            return [body]
        _state, name, address, port = decision
        return self._proxy(environ, start_response, host, name, address, port)

    def _admit(self) -> bool:
        limiter = self.app.extensions.get("bananawiki.hosting.limiter")
        return limiter is None or limiter.hit(f"wikihost:{web.client_network()}", RATE_LIMIT, 60)

    # Which wiki, in which state

    def _decide(self, host: str) -> tuple | None:
        """None: not a wiki host (the portal answers). Otherwise a page to show or a proxy target."""
        from . import domains, instances, urls
        from .db import db

        cfg = self.app.config["HOSTING"]
        if host.endswith("." + cfg.base_domain):
            found = domains.platform_slug(host)
            if found is None or found[0] in urls.reserved():
                return None
            inst = instances.by_slug(*found)
            if inst is None:
                return ("missing", host)
        else:
            instance_id = db.scalar(
                "SELECT d.instance_id FROM instance_custom_domains d JOIN instances i ON i.id = d.instance_id "
                "JOIN accounts a ON a.id = i.account_id WHERE d.domain = ? AND d.verified_at IS NOT NULL "
                "AND d.verified_until > ? AND " + domains.allowed_sql(), (host, now_sql()))
            inst = instances.get(instance_id) if instance_id else None
            if inst is None:
                return None
        row = db.one("SELECT i.*, a.suspended AS account_suspended, a.deleted_at AS account_deleted, "
                     "a.approval_status, a.pending_deletion FROM instances i JOIN accounts a ON a.id = i.account_id "
                     "WHERE i.id = ?", (inst["id"],))
        if row is None or row["status"] == "terminated":
            return ("missing", host)
        if row["status"] == "suspended":
            return ("suspended", host, row)
        if (row["account_suspended"] or row["account_deleted"] or row["approval_status"] != "approved"
                or row["pending_deletion"]):
            return ("unavailable", host)
        if row["expires_at"] and is_past(row["expires_at"]):
            return ("expired", host)
        if row["status"] != "running":
            return ("paused", host)
        spec = instances.spec(row, with_policy=False)
        try:
            healthy = instances.runtime().status(spec).healthy
            target = instances.runtime().upstream(spec) if healthy else None
        except (RuntimeFailure, AttributeError, TypeError) as error:
            log.warning("Cannot locate wiki %s: %s", spec.data_dir_name, error)
            target = None
        if target is None:
            return ("starting", host)
        return ("proxy", spec.data_dir_name, target[0], int(target[1]))

    def _page(self, state: str, host: str, row: dict[str, Any] | None = None,
              code: int | None = None) -> tuple[str, list[tuple[str, str]], bytes]:
        from . import urls

        cfg = self.app.config["HOSTING"]
        # Never 502 or 504: Cloudflare replaces those with its own "Bad gateway" page,
        # hiding this one and its automatic refresh.
        codes = {"missing": 404, "suspended": 403, "unavailable": 503, "expired": 503, "paused": 503,
                 "starting": 503, "busy": 503, "timeout": 503, "bad_gateway": 503, "limited": 429}
        status = code or codes.get(state, 503)
        details: dict[str, Any] = {}
        if state == "suspended" and row:
            if row.get("suspend_reason") and row.get("suspend_reason_visible"):
                details["reason"] = row["suspend_reason"]
            if row.get("suspended_until") and row.get("suspend_time_visible"):
                details["until"] = row["suspended_until"]
        refresh = state in ("starting", "busy", "bad_gateway", "timeout")
        # The page reloads itself through a small script (only while it is in front of the visitor, who can
        # stop it), with a <noscript> meta refresh for browsers without scripts.
        nonce = secrets.token_urlsafe(16) if refresh else ""
        body = render_template("hosting/wiki_status.html", state=state, host=host, details=details,
                               refresh=refresh, nonce=nonce,
                               portal_url=urls.portal_base_url(), contact=cfg.contact_email)
        reasons = {403: "Forbidden", 404: "Not Found", 429: "Too Many Requests", 502: "Bad Gateway",
                   503: "Service Unavailable", 504: "Gateway Timeout"}
        headers = [
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
            ("Content-Security-Policy", PAGE_CSP + (f"; script-src 'nonce-{nonce}'" if nonce else "")),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
        ]
        if status == 503:
            headers.append(("Retry-After", "5"))
        elif status == 429:
            headers.append(("Retry-After", "60"))
        encoded = body.encode("utf-8")
        headers.append(("Content-Length", str(len(encoded))))
        return f"{status} {reasons.get(status, 'Error')}", headers, encoded

    def _error(self, environ: dict[str, Any], start_response: Any, host: str, state: str) -> list[bytes]:
        with self.app.request_context(environ):
            status, headers, body = self._page(state, host)
        start_response(status, headers)
        return [body]

    # Proxying

    def _proxy(self, environ: dict[str, Any], start_response: Any, host: str, name: str, address: str,
               port: int) -> Iterable[bytes]:
        slot = self.slots.get(name)
        if not slot.acquire(timeout=QUEUE_SECONDS):
            return self._error(environ, start_response, host, "busy")
        connection: http.client.HTTPConnection | None = None
        try:
            method = environ.get("REQUEST_METHOD", "GET")
            connection = self.slots.connect(address, port)
            connection.putrequest(method, _target(environ), skip_host=True, skip_accept_encoding=True)
            body = _request_body(environ)
            for key, value in _request_headers(environ, host):
                connection.putheader(key, value)
            if isinstance(body, bytes):
                connection.putheader("Content-Length", str(len(body)))
            elif body is not None:
                connection.putheader("Content-Length", environ["CONTENT_LENGTH"])
            elif method not in ("GET", "HEAD", "OPTIONS", "DELETE"):
                connection.putheader("Content-Length", "0")
            if body is not None and not isinstance(body, bytes):
                connection.endheaders()
                connection.sock.settimeout(READ_TIMEOUT)
                for chunk in body:
                    connection.send(chunk)
            else:
                connection.endheaders(body or None)
                connection.sock.settimeout(READ_TIMEOUT)
            response = connection.getresponse()
        except BodyTooLarge:
            _close(connection)
            slot.release()
            message = b"The request body is too large.\n"
            start_response("413 Payload Too Large", [("Content-Type", "text/plain; charset=utf-8"),
                                                     ("Content-Length", str(len(message))), ("Connection", "close")])
            return [message]
        except TimeoutError:
            _close(connection)
            slot.release()
            return self._error(environ, start_response, host, "timeout")
        except (OSError, http.client.HTTPException, ValueError) as error:
            # ValueError: anything else http.client refuses to send (a request line or header it cannot encode).
            log.warning("Proxy to wiki %s (%s:%s) failed: %s", name, address, port, error)
            _close(connection)
            slot.release()
            return self._error(environ, start_response, host, "bad_gateway")
        except BaseException:
            _close(connection)
            slot.release()
            raise
        # A wiki's own code (a plugin) could answer with a header the server refuses (a folded line, a
        # control character): it is left out, and nothing taken for this request is ever left held.
        headers = [(key, value) for key, value in response.getheaders()
                   if key.lower() not in HOP_BY_HOP and _HEADER_NAME.fullmatch(key) and not _BAD_VALUE.search(value)]
        try:
            start_response(_status_line(response.status), headers)
        except BaseException:
            response.close()
            _close(connection)
            slot.release()
            raise
        if method == "HEAD":
            response.close()
            _close(connection)
            slot.release()
            return []
        return _Body(response, connection, slot, self.slots.draining)


_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_BAD_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def _status_line(code: int) -> str:
    """The status line for *code* with its standard phrase: the wiki's own reason text is not passed on
    (it could hold a bare CR)."""
    try:
        return f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        return f"{code} Unknown"


def _close(connection: http.client.HTTPConnection | None) -> None:
    if connection is not None:
        connection.close()


_PRINTABLE = "".join(chr(code) for code in range(0x21, 0x7f))


def _ascii(text: str) -> str:
    """*text* with every character outside printable ASCII percent-encoded, as the bytes the client sent
    (the server hands them over decoded as Latin-1): http.client sends only ASCII request lines."""
    if text.isascii() and text.isprintable() and " " not in text:
        return text
    try:
        sent = text.encode("latin-1")
    except UnicodeEncodeError:
        sent = text.encode("utf-8", "surrogateescape")
    return quote(sent, safe=_PRINTABLE)


def _target(environ: dict[str, Any]) -> str:
    """The request target as the client sent it (origin form), in ASCII."""
    raw = environ.get("RAW_URI") or environ.get("REQUEST_URI") or ""
    if raw.startswith("/") and not raw.startswith("//") and not any(c in raw for c in "\r\n "):
        return _ascii(raw)
    path = (environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", "")).encode("latin-1", "replace")
    target = quote(path, safe="/:@!$&'()*+,;=~-._%") or "/"
    query = _ascii(environ.get("QUERY_STRING", ""))
    return f"{target}?{query}" if query else target


def _request_headers(environ: dict[str, Any], host: str) -> list[tuple[str, str]]:
    headers = []
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").title()
            if name.lower() not in STRIPPED_REQUEST:
                headers.append((name, value))
    if environ.get("CONTENT_TYPE"):
        headers.append(("Content-Type", environ["CONTENT_TYPE"]))
    original = environ.get("HTTP_HOST") or host
    headers += [
        ("Host", original),
        # The tenant trusts exactly one proxy hop (BW_PROXY_MODE): this one.
        ("X-Forwarded-For", environ.get("REMOTE_ADDR") or "127.0.0.1"),
        ("X-Forwarded-Proto", environ.get("wsgi.url_scheme", "http")),
        ("X-Forwarded-Host", original),
        ("Connection", "close"),
    ]
    return headers


class BodyTooLarge(Exception):
    """A request body without a declared length passed MAX_BUFFERED_BODY."""


def _request_body(environ: dict[str, Any]) -> bytes | Iterator[bytes] | None:
    """Stream a body of known length; buffer a chunked one (bounded)."""
    stream = environ.get("wsgi.input")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if stream is None:
        return None
    if length > 0:
        def chunks() -> Iterator[bytes]:
            remaining = length
            while remaining > 0:
                chunk = stream.read(min(CHUNK, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk

        return chunks() if length > CHUNK else stream.read(length)
    if "chunked" in (environ.get("HTTP_TRANSFER_ENCODING") or "").lower():
        data = stream.read(MAX_BUFFERED_BODY + 1)
        if len(data) > MAX_BUFFERED_BODY:
            raise BodyTooLarge
        return data
    return None


def install(app: Flask) -> None:
    app.wsgi_app = WikiHosts(app, app.wsgi_app)  # type: ignore[method-assign]


__all__ = ["WikiHosts", "install", "request_host"]
