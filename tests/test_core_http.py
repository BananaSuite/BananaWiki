"""core.http: SSRF guards, size and time limits, redirect policy."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from bananawiki.core import http


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path == "/big":
            self._send(200, b"x" * 5000)
        elif self.path == "/malformed-size":
            self.send_response(200)
            self.send_header("Content-Length", "9" * 5000)
            self.end_headers()
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
    ("fe80::1", True, False), ("100.64.0.1", False, False), ("fec0::1", False, False), ("fec0::1", True, False),
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


def test_an_unparseable_response_length_raises_http_error(server):
    with pytest.raises(http.HttpError):
        _get(server + "/malformed-size")


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


@pytest.mark.parametrize("url", ["https://[", "https://[not-an-ip]/", "https://example.com:0/"])
def test_malformed_hosts_and_ports_raise_a_destination_error(url):
    with pytest.raises(http.BlockedDestination):
        http.check_url(url)


def test_connection_attempts_share_the_total_time_budget(monkeypatch):
    """Multiple failed DNS addresses must not each get the whole deadline."""
    elapsed = [0.0]
    attempts = []
    monkeypatch.setattr(http.time, "monotonic", lambda: elapsed[0])

    class FailingSocket:
        def __init__(self, *_args):
            self.timeout = None

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            attempts.append(address)
            elapsed[0] += min(0.6, self.timeout)
            raise TimeoutError("connection timed out")

        def shutdown(self, _how):
            pass

        def close(self):
            pass

    monkeypatch.setattr(http.socket, "socket", FailingSocket)

    def resolver(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.36", port))]

    with pytest.raises(http.HttpError):
        http.request("GET", "http://deadline.example/", schemes=("http",), resolver=resolver,
                     timeout=1, total_timeout=1)
    assert elapsed[0] <= 1.0
    assert len(attempts) == 2


def test_https_pins_the_address_and_verifies_the_original_host(tmp_path, monkeypatch):
    """Connecting to a checked IP must retain hostname certificate validation."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tls.example")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("tls.example")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "server.pem", tmp_path / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    monkeypatch.setattr(http.ssl, "create_default_context", lambda: client_context)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.socket = server_context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    resolved = []

    def resolver(host, requested_port):
        resolved.append((host, requested_port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    try:
        response = http.request("GET", f"https://tls.example:{port}/hello", allow_private=True,
                                resolver=resolver)
        assert response.status == 200 and response.body == b"hello"
        with pytest.raises(http.HttpError):
            http.request("GET", f"https://wrong.example:{port}/hello", allow_private=True,
                         resolver=resolver)
        assert resolved == [("tls.example", port), ("wrong.example", port)]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_total_deadline_also_bounds_dns_resolution():
    """A stuck DNS resolver must release the request without opening a socket."""
    entered, release = threading.Event(), threading.Event()

    def resolver(_host, port):
        entered.set()
        assert release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def request():
        with pytest.raises(http.HttpError):
            http.request("GET", "https://slow-dns.example/", resolver=resolver,
                         timeout=0.05, total_timeout=0.05)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(request)
        try:
            assert entered.wait(5)
            try:
                future.result(timeout=1)
            except FutureTimeout:
                pytest.fail("the HTTP request remained blocked on DNS past its deadline")
        finally:
            release.set()
