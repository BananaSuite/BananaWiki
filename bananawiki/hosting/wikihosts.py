"""Requests for a wiki's hostname that reach the portal instead of the wiki.

Caddy normally sends each wiki host straight to its container (the runtime
agent writes those routes). A wiki host still ends up here when that route is
missing: the Caddyfile was written by hand without the routes import, the
server still uses the 1.4 proxy configuration that sends every host to the
portal, the agent cannot publish routes, or the container has only just
(re)started. Without this middleware the portal answered such requests as if
they were its own, so opening a wiki showed the portal's sign-in page or
dashboard instead.

For a wiki host this middleware never lets the portal application run:

* a running, healthy wiki is proxied (as 1.4's portal did), so it works even
  without per-wiki routes;
* anything else gets a small status page on the wiki's own address: no wiki
  here, paused, suspended, expired, or starting (refreshes by itself).

Hosts that are not wiki hosts (the portal and base domains, addresses,
``localhost``, reserved labels such as ``www``) go to the portal unchanged.
"""

from __future__ import annotations

import http.client
import logging
import os
import threading
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import quote

from flask import Flask, render_template

from ..core.timeutil import is_past, now_sql
from .runtime import RuntimeFailure

log = logging.getLogger("bananawiki.hosting.wikihosts")

CONNECT_TIMEOUT = 5
# Below the portal's worker timeout (HOSTING_WORKER_TIMEOUT, 120 s), so a slow
# wiki gets a clean status page instead of a killed worker.
READ_TIMEOUT = 100
CHUNK = 64 * 1024
# Portal workers have few threads (HOSTING_THREADS, default 4): proxied wiki
# traffic never takes the last one, and one busy wiki not even that many.
PER_WIKI = 3
QUEUE_SECONDS = 10
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
    try:
        return max(1, min(64, int(os.environ.get("HOSTING_THREADS") or 4)))
    except ValueError:
        return 4


class _Slot:
    """One proxied request: a place for its wiki and one in the process-wide pool."""

    def __init__(self, wiki: threading.BoundedSemaphore, pool: threading.BoundedSemaphore):
        self._wiki = wiki
        self._pool = pool

    def acquire(self, timeout: float) -> bool:
        if not self._wiki.acquire(timeout=timeout):
            return False
        if not self._pool.acquire(timeout=timeout):
            self._wiki.release()
            return False
        return True

    def release(self) -> None:
        self._pool.release()
        self._wiki.release()


class _Slots:
    """Bounded concurrent proxied requests, per wiki and per portal process."""

    def __init__(self, total: int | None = None) -> None:
        total = total or max(1, _thread_count() - 1)
        self._per_wiki = min(PER_WIKI, total)
        self._pool = threading.BoundedSemaphore(total)
        self._lock = threading.Lock()
        self._slots: dict[str, threading.BoundedSemaphore] = {}

    def get(self, name: str) -> _Slot:
        with self._lock:
            slot = self._slots.get(name)
            if slot is None:
                slot = self._slots[name] = threading.BoundedSemaphore(self._per_wiki)
            return _Slot(slot, self._pool)


class _Body:
    """The upstream response body; closing it frees the connection and the slot."""

    def __init__(self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection,
                 slot: _Slot):
        self._response = response
        self._connection = connection
        self._slot: _Slot | None = slot

    def __iter__(self) -> Iterator[bytes]:
        while True:
            try:
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
            decision = self._decide(host)
            page = None if decision is None or decision[0] == "proxy" else self._page(*decision)
        if decision is None:
            return self.wsgi_app(environ, start_response)
        if page is not None:
            status, headers, body = page
            start_response(status, headers)
            return [body]
        _state, name, address, port = decision
        return self._proxy(environ, start_response, host, name, address, port)

    # ── Which wiki, in which state ────────────────────────────────────────

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
                "WHERE d.domain = ? AND d.verified_at IS NOT NULL AND d.verified_until > ? "
                "AND i.custom_domain_allowed = 1", (host, now_sql()))
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
        # hiding this one and its automatic refresh (1.4 avoided them for the same reason).
        codes = {"missing": 404, "suspended": 403, "unavailable": 503, "expired": 503, "paused": 503,
                 "starting": 503, "busy": 503, "timeout": 503, "bad_gateway": 503}
        status = code or codes.get(state, 503)
        details: dict[str, Any] = {}
        if state == "suspended" and row:
            if row.get("suspend_reason") and row.get("suspend_reason_visible"):
                details["reason"] = row["suspend_reason"]
            if row.get("suspended_until") and row.get("suspend_time_visible"):
                details["until"] = row["suspended_until"]
        body = render_template("hosting/wiki_status.html", state=state, host=host, details=details,
                               refresh=state in ("starting", "busy", "bad_gateway", "timeout"),
                               portal_url=urls.portal_base_url(), contact=cfg.contact_email)
        reasons = {403: "Forbidden", 404: "Not Found", 502: "Bad Gateway", 503: "Service Unavailable",
                   504: "Gateway Timeout"}
        headers = [
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
            ("Content-Security-Policy", PAGE_CSP), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
        ]
        if status == 503:
            headers.append(("Retry-After", "5"))
        encoded = body.encode("utf-8")
        headers.append(("Content-Length", str(len(encoded))))
        return f"{status} {reasons.get(status, 'Error')}", headers, encoded

    def _error(self, environ: dict[str, Any], start_response: Any, host: str, state: str) -> list[bytes]:
        with self.app.request_context(environ):
            status, headers, body = self._page(state, host)
        start_response(status, headers)
        return [body]

    # ── Proxying ──────────────────────────────────────────────────────────

    def _proxy(self, environ: dict[str, Any], start_response: Any, host: str, name: str, address: str,
               port: int) -> Iterable[bytes]:
        slot = self.slots.get(name)
        if not slot.acquire(timeout=QUEUE_SECONDS):
            return self._error(environ, start_response, host, "busy")
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection(address, port, timeout=CONNECT_TIMEOUT)
            method = environ.get("REQUEST_METHOD", "GET")
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
        except TimeoutError:
            _close(connection)
            slot.release()
            return self._error(environ, start_response, host, "timeout")
        except (OSError, http.client.HTTPException) as error:
            log.warning("Proxy to wiki %s (%s:%s) failed: %s", name, address, port, error)
            _close(connection)
            slot.release()
            return self._error(environ, start_response, host, "bad_gateway")
        except BaseException:
            _close(connection)
            slot.release()
            raise
        headers = [(key, value) for key, value in response.getheaders() if key.lower() not in HOP_BY_HOP]
        start_response(f"{response.status} {response.reason}", headers)
        if method == "HEAD":
            response.close()
            _close(connection)
            slot.release()
            return []
        return _Body(response, connection, slot)


def _close(connection: http.client.HTTPConnection | None) -> None:
    if connection is not None:
        connection.close()


def _target(environ: dict[str, Any]) -> str:
    """The request target as the client sent it (origin form)."""
    raw = environ.get("RAW_URI") or environ.get("REQUEST_URI") or ""
    if raw.startswith("/") and not raw.startswith("//") and not any(c in raw for c in "\r\n "):
        return raw
    path = (environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", "")).encode("latin-1", "replace")
    target = quote(path, safe="/:@!$&'()*+,;=~-._%") or "/"
    query = environ.get("QUERY_STRING", "")
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
        return data[:MAX_BUFFERED_BODY]
    return None


def install(app: Flask) -> None:
    app.wsgi_app = WikiHosts(app, app.wsgi_app)  # type: ignore[method-assign]


__all__ = ["WikiHosts", "install", "request_host"]
