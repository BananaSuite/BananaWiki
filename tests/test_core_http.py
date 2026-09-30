"""core.http: SSRF guards, size and time limits, redirect policy."""

from __future__ import annotations

import ipaddress
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bananawiki.core import http


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path == "/big":
            self._send(200, b"x" * 5000)
        elif self.path == "/away":
            self.send_response(302)
            self.send_header("Location", "http://other.example/steal")
            self.end_headers()
        elif self.path == "/here":
            self.send_response(302)
            self.send_header("Location", "/hello")
            self.end_headers()
        else:
            self._send(200, b"hello", {"X-Test": "yes"})

    def _send(self, status, body, headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url, **options):
    return http.request("GET", url, schemes=("http",), allow_private=True, **options)


@pytest.mark.parametrize("address, private_ok, allowed", [
    ("93.184.216.34", False, True), ("10.0.0.1", False, False), ("10.0.0.1", True, True),
    ("127.0.0.1", False, False), ("127.0.0.1", True, True), ("169.254.169.254", True, False),
    ("0.0.0.0", True, False), ("224.0.0.1", True, False), ("::ffff:127.0.0.1", False, False),
    ("fe80::1", True, False), ("100.64.0.1", False, False),
])
def test_address_policy(address, private_ok, allowed):
    assert http.address_allowed(ipaddress.ip_address(address), allow_private=private_ok) is allowed


@pytest.mark.parametrize("url", [
    "http://example.com/", "https://user:pw@example.com/", "https://example.com/#frag", "ftp://example.com/",
    "https://exa mple.com/", "https://example.com:99999/", "javascript:alert(1)",
])
def test_bad_urls_are_refused(url):
    with pytest.raises(http.BlockedDestination):
        http.request("GET", url)


def test_every_resolved_address_is_checked():
    def resolver(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]

    with pytest.raises(http.BlockedDestination):
        http.request("GET", "https://rebinding.example/", resolver=resolver)


def test_loopback_is_refused_by_default(server):
    with pytest.raises(http.BlockedDestination):
        http.request("GET", server + "/hello", schemes=("http",))


def test_allowed_private_request_works(server):
    response = _get(server + "/hello")
    assert response.ok and response.body == b"hello" and response.header("X-TEST") == "yes"


def test_response_size_is_capped(server):
    with pytest.raises(http.HttpError):
        _get(server + "/big", max_bytes=1000)


def test_redirects_are_not_followed_by_default_and_never_to_other_hosts(server):
    assert _get(server + "/here").status == 302
    assert _get(server + "/here", max_redirects=2).body == b"hello"
    away = _get(server + "/away", max_redirects=2)
    assert away.status == 302 and away.header("location") == "http://other.example/steal"


def test_connection_failures_raise_http_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(http.HttpError):
        _get(f"http://127.0.0.1:{port}/", timeout=2, total_timeout=3)


def test_limits_are_validated():
    with pytest.raises(ValueError):
        http.request("GET", "https://example.com/", timeout=0)
