"""Outbound HTTP for features that call other servers (federation, OAuth, remote services).

Every request made on behalf of configuration an administrator typed in, or
of data a remote party controls, goes through :func:`request`, which guards
against server-side request forgery and resource exhaustion:

* Only the schemes the caller allows (HTTPS by default); no credentials or
  fragments in the URL.
* The host name is resolved once and every address is checked before a
  connection is opened; the socket then connects to those checked addresses,
  so a DNS answer cannot change between the check and the connection. TLS
  still verifies the certificate against the host name.
* Private, loopback and shared addresses are refused unless the caller
  explicitly allows them (for example a peer an administrator marked as
  being on the local network). Link-local (cloud metadata), deprecated IPv6
  site-local (``fec0::/10``, which :mod:`ipaddress` counts as global),
  multicast, unspecified and reserved addresses are always refused.
* Redirects are not followed unless asked for, and then only to the same
  scheme, host and port.
* Connect/read timeouts, a total deadline that also stops servers that trickle
  bytes, and a cap on the response size.
* Environment proxies are ignored; requests go directly to the checked address.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
MAX_URL_LENGTH = 8192
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
# RFC 6598 shared address space (carrier-grade NAT, Tailscale): neither global nor "private" to ipaddress.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
_DROPPED_HEADERS = frozenset({"host", "connection", "proxy-authorization", "content-length", "transfer-encoding"})

Resolver = Callable[[str, int], list[tuple]]


class HttpError(Exception):
    """The request could not be completed (network, timeout, size or protocol problem)."""


class BlockedDestination(HttpError):
    """The URL or the address it resolves to is not allowed."""


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""
    url: str = ""

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _system_resolver(host: str, port: int) -> list[tuple]:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def address_allowed(address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool) -> bool:
    """Whether a connection to *address* may be opened."""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if (address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved
            or getattr(address, "is_site_local", False)):
        return False
    if address.is_global:
        return True
    return allow_private and (address.is_private or address.is_loopback or address in _SHARED_ADDRESS_SPACE)


def check_url(url: str, *, schemes: tuple[str, ...] = ("https",)) -> tuple[str, str, int]:
    """Validate the shape of *url*; return (scheme, host, port)."""
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise BlockedDestination("The address is not a valid URL.")
    parts = urlsplit(url)
    if parts.scheme not in schemes or not parts.hostname:
        raise BlockedDestination("The address uses a scheme that is not allowed.")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise BlockedDestination("The address must not contain credentials or a fragment.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise BlockedDestination("The address has an invalid port.") from None
    try:
        host = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise BlockedDestination("The address has an invalid host name.") from None
    return parts.scheme, host, port


def resolve(host: str, port: int, *, allow_private: bool, resolver: Resolver | None = None) -> list[tuple]:
    """Resolve *host* once and refuse it when any address is not allowed."""
    try:
        infos = (resolver or _system_resolver)(host, port)
    except OSError as error:
        raise HttpError("The server name could not be resolved.") from error
    if not infos:
        raise HttpError("The server name has no address.")
    for family, _kind, _proto, _canon, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise BlockedDestination("The server resolved to an unsupported address.")
        address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        if not address_allowed(address, allow_private=allow_private):
            raise BlockedDestination("The server resolved to an address that is not allowed.")
    return infos


def _dial(infos: list[tuple], timeout: float) -> socket.socket:
    last: OSError | None = None
    for family, kind, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as error:
            last = error
            sock.close()
    raise last or OSError("connection failed")


class _Deadline:
    """Shut the socket down when the total time budget runs out."""

    def __init__(self, seconds: float):
        self.expired = threading.Event()
        self._sock: socket.socket | None = None
        self._timer = threading.Timer(max(0.0, seconds), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, sock: socket.socket | None) -> None:
        self._sock = sock

    def _fire(self) -> None:
        self.expired.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def cancel(self) -> None:
        self._timer.cancel()


def _read_body(response: http.client.HTTPResponse, max_bytes: int, deadline: _Deadline) -> bytes:
    declared = response.getheader("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HttpError("The server response is too large.")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(65536, max_bytes + 1 - total))
        if deadline.expired.is_set():
            raise HttpError("The server did not answer in time.")
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise HttpError("The server response is too large.")
        chunks.append(chunk)


def _send_once(method: str, url: str, headers: Mapping[str, str], body: bytes | None, *, timeout: float,
               deadline: _Deadline, max_bytes: int, allow_private: bool, schemes: tuple[str, ...],
               resolver: Resolver | None) -> HttpResponse:
    scheme, host, port = check_url(url, schemes=schemes)
    infos = resolve(host, port, allow_private=allow_private, resolver=resolver)
    if scheme == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    # Connect to the checked addresses; TLS still verifies against the host name.
    connection._create_connection = lambda *_args, **_kwargs: _dial(infos, timeout)  # type: ignore[attr-defined]
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    clean = {name: value for name, value in headers.items() if name.lower() not in _DROPPED_HEADERS}
    clean.setdefault("User-Agent", "BananaWiki")
    clean["Connection"] = "close"
    try:
        connection.connect()
        deadline.watch(connection.sock)
        connection.request(method, target, body=body, headers=clean)
        response = connection.getresponse()
        payload = _read_body(response, max_bytes, deadline)
        return HttpResponse(
            status=response.status,
            headers={name.lower(): value for name, value in response.getheaders()},
            body=payload,
            url=url,
        )
    except HttpError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError) as error:
        if deadline.expired.is_set():
            raise HttpError("The server did not answer in time.") from error
        raise HttpError("The connection to the server failed.") from error
    finally:
        connection.close()


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 10.0,
    total_timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_private: bool = False,
    schemes: tuple[str, ...] = ("https",),
    max_redirects: int = 0,
    resolver: Resolver | None = None,
) -> HttpResponse:
    """Send one request and return the whole (bounded) response.

    Non-2xx answers are returned, not raised; network, timeout, size and
    destination problems raise :class:`HttpError` (or :class:`BlockedDestination`).
    """
    if not 0 < timeout <= 600 or not 0 < total_timeout <= 3600 or not 0 < max_bytes <= 1024 ** 3:
        raise ValueError("Invalid timeout or size limit.")
    started = time.monotonic()
    current_url, current_method, current_body = url, method.upper(), body
    origin = check_url(url, schemes=schemes)
    for _hop in range(max_redirects + 1):
        remaining = total_timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise HttpError("The server did not answer in time.")
        deadline = _Deadline(remaining)
        try:
            response = _send_once(
                current_method, current_url, headers or {}, current_body,
                timeout=min(timeout, remaining), deadline=deadline, max_bytes=max_bytes,
                allow_private=allow_private, schemes=schemes, resolver=resolver,
            )
        finally:
            deadline.cancel()
        location = response.header("location")
        if response.status not in _REDIRECT_CODES or not location or _hop == max_redirects:
            return response
        next_url = urljoin(current_url, location)
        if check_url(next_url, schemes=schemes) != origin:
            return response  # never follow a redirect to another host
        if response.status == 303 or (response.status in (301, 302) and current_method == "POST"):
            current_method, current_body = "GET", None
        current_url = next_url
    return response
