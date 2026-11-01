"""Regression tests for the LW2 findings of the full re-audit pass 6 (desktop servers, contrib speech server)."""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
import time

import pytest

from bananawiki.core.downloads import Places
from bananawiki.desktop import server
from tests.features.test_tts_gpu import FakeEngine, load_server

# LW2-1: the GPU speech server escapes control characters in what it logs of a request line


def _raw_request(port: int, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(data)
        return sock.recv(200)


def test_lw2_1_speech_server_logs_request_lines_with_control_characters_escaped(caplog):
    module = load_server()
    speech = module.make_server(module.Settings(token="x" * 32, port=0), FakeEngine())
    threading.Thread(target=speech.serve_forever, daemon=True).start()
    caplog.set_level(logging.INFO, logger="bananawiki-tts-gpu")
    try:
        port = speech.server_address[1]
        # No token needed: OSC 0 retitles the terminal, OSC 52 writes its clipboard, \x9b is a C1 CSI.
        assert _raw_request(port, b"GET /health\x1b]0;all good\x07 HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP")
        assert _raw_request(port, b"GET /\x1b]52;c;ZWNobw==\x07\x9b2J HTTP/1.1 junk\r\n\r\n")  # an HTTP/0.9 error page
        authorized = b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\nAuthorization: Bearer " + b"x" * 32 + b"\r\n\r\n"
        assert _raw_request(port, authorized).startswith(b"HTTP/1.1 200")
    finally:
        speech.shutdown()
        speech.server_close()
    logged = [record.getMessage() for record in caplog.records if record.name == "bananawiki-tts-gpu"]
    assert not [line for line in logged if any(char in line for char in "\x1b\x07\x9b\r\n")]
    assert any("/health\\x1b]0;all good\\x07 HTTP/1.1" in line for line in logged)
    assert any("\\x1b]52;c;ZWNobw==\\x07\\x9b2J" in line for line in logged)
    # The tenant field stays (KW2-3), the token is never logged.
    assert any(line.split()[1] == "operator" for line in logged)
    assert all(line.split()[1] in {"-", "operator"} for line in logged)
    assert not any("x" * 32 in line for line in logged)


# LW2-2 and LW2-3: the desktop launcher's waitress server (the real runner, with a small application)

MIB = 1024 * 1024


class _Download:
    """A 64 MiB body that holds a download place until the server closes it."""

    def __init__(self, release, closed: threading.Event):
        self._release, self._closed = release, closed

    def __iter__(self):
        chunk = b"x" * MIB
        for _ in range(64):
            yield chunk

    def close(self):
        self._release()
        self._closed.set()


class _App:
    extensions: dict = {}  # what _WaitressRunner.stop() looks into

    def __init__(self):
        self.places = Places(1, 1)
        self.closed = threading.Event()

    def __call__(self, environ, start_response):
        path = environ["PATH_INFO"]
        if path == "/download":
            release, status = self.places.take("pupil")
            if release is None:
                start_response(f"{status} Busy", [("Content-Length", "0")])
                return [b""]
            start_response("200 OK", [("Content-Type", "application/octet-stream")])
            return _Download(release, self.closed)
        if path == "/slow":
            time.sleep(1.2)  # working, with nothing to send yet
        elif path == "/upload":
            environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
        start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
        return [b"ok"]


@pytest.fixture
def waitress(monkeypatch):
    """Start the launcher's waitress runner on a free loopback port, with short limits."""
    runners = []

    def start(header_timeout: float = 0.5):
        monkeypatch.setattr(server, "HEADER_TIMEOUT", header_timeout, raising=False)
        monkeypatch.setattr(server, "WRITE_TIMEOUT", 0.5, raising=False)
        monkeypatch.setattr(server, "CLEANUP_INTERVAL", 0, raising=False)
        monkeypatch.setattr(server, "CONNECTIONS_PER_ADDRESS", 3, raising=False)
        app = _App()
        runners.append(server._WaitressRunner(app, "127.0.0.1", [0], "nonce"))
        return runners[-1], app

    yield start
    for runner in runners:
        runner.stop()


def _connect(port: int, source: str | None = None) -> socket.socket:
    sock = socket.socket()
    sock.settimeout(5)
    if source:
        try:
            sock.bind((source, 0))
        except OSError:
            sock.close()
            pytest.skip(f"cannot send from {source} here")
    sock.connect(("127.0.0.1", port))
    return sock


def _closed_within(sock: socket.socket, seconds: float) -> bool:
    """Whether the server closes *sock* within *seconds* (whatever it sent before is read and dropped)."""
    deadline = time.monotonic() + seconds
    try:
        while (left := deadline - time.monotonic()) > 0:
            sock.settimeout(left)
            if not sock.recv(65536):
                return True
    except TimeoutError:
        return False
    except ConnectionError:
        return True
    return False


def _status(port: int, request: bytes) -> bytes:
    with _connect(port) as sock:
        sock.sendall(request)
        return sock.recv(100).split(b"\r\n", 1)[0]


def test_lw2_3_waitress_drops_a_download_its_client_stopped_reading(waitress):
    runner, app = waitress()
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.settimeout(5)
    sock.connect(("127.0.0.1", runner.port))
    try:
        sock.sendall(b"GET /download HTTP/1.1\r\nHost: x\r\n\r\n")
        assert sock.recv(12) == b"HTTP/1.1 200"
        # Reads nothing more: the application thread waits for room in waitress's output buffer.
        assert app.closed.wait(4), "the response (and its download place) is still held"
        assert app.places.active == 0
        assert _closed_within(sock, 3)
    finally:
        sock.close()
    # The place is free for the next download, which a reading client gets whole.
    with _connect(runner.port) as sock:
        sock.sendall(b"GET /download HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        received = b"".join(iter(lambda: sock.recv(MIB), b""))
    assert received.startswith(b"HTTP/1.1 200 OK") and received.endswith(b"x\r\n0\r\n\r\n")  # chunked, complete
    assert received.count(b"x") >= 64 * MIB


def test_lw2_3_waitress_keeps_a_response_that_is_still_being_worked_on(waitress):
    runner, _app = waitress()
    assert _status(runner.port, b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n") == b"HTTP/1.1 200 OK"


def _trickle(sock: socket.socket, still_served, seconds: float = 4) -> float:
    """Send a request's headers a byte at a time while *still_served()*: how long until they stopped being taken."""
    started = time.monotonic()
    with contextlib.suppress(OSError):
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nX-Slow: ")
        while still_served() and time.monotonic() - started < seconds:
            sock.sendall(b"a")  # waitress's channel_timeout counts from the last byte received
    return time.monotonic() - started


def test_lw2_2_waitress_drops_a_client_that_trickles_its_headers(waitress):
    runner, _app = waitress()
    with _connect(runner.port) as sock:
        assert _trickle(sock, lambda: not _closed_within(sock, 0.2)) < 3, "the trickling client kept its connection"


def test_lw2_2_waitress_drops_a_connection_that_sends_nothing(waitress):
    runner, _app = waitress()
    with _connect(runner.port) as sock:
        assert _closed_within(sock, 2.5)


def test_lw2_2_waitress_lets_slow_but_complete_headers_and_bodies_through(waitress):
    runner, _app = waitress()
    with _connect(runner.port) as sock:
        sock.sendall(b"POST /upload HTTP/1.1\r\nHost: x\r\n")
        time.sleep(0.2)
        sock.sendall(b"Content-Length: 4\r\n\r\n")
        for byte in b"body":  # the body may take longer than the header deadline
            time.sleep(0.3)
            sock.sendall(bytes([byte]))
        assert sock.recv(100).startswith(b"HTTP/1.1 200")


def test_lw2_2_waitress_caps_the_connections_of_one_address(waitress):
    runner, _app = waitress(header_timeout=30)
    held = [_connect(runner.port, "127.0.0.2") for _ in range(3)]
    try:
        for sock in held:
            sock.sendall(b"GET / HTTP/1.1\r\n")
        extra = _connect(runner.port, "127.0.0.2")
        with extra:
            assert _closed_within(extra, 1), "a fourth connection from the same address was kept"
        # Another address (the teacher's browser on loopback) is still served.
        assert _status(runner.port, b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n") == b"HTTP/1.1 200 OK"
    finally:
        for sock in held:
            sock.close()


# LW2-2: the gunicorn child (the real gthread worker's connection handling, with the child's post_worker_init)


@pytest.fixture
def gthread(monkeypatch):
    """Start a gthread worker set up by the child's post_worker_init; serve(client) hands it one connection."""
    pytest.importorskip("gunicorn")
    from gunicorn.config import Config
    from gunicorn.glogging import Logger
    from gunicorn.workers.gthread import TConn, ThreadWorker

    from bananawiki.desktop import gunicorn_child

    listener = socket.create_server(("127.0.0.1", 0))
    workers = []

    def start(header_timeout: float = 0.5):
        monkeypatch.setattr(server, "HEADER_TIMEOUT", header_timeout, raising=False)
        monkeypatch.setattr(server, "HEADER_READERS_PER_ADDRESS", 2, raising=False)
        cfg = Config()
        cfg.set("keepalive", 0)
        cfg.set("threads", 4)
        worker = ThreadWorker(1, os.getpid(), [], None, 30, cfg, Logger(cfg))
        workers.append(worker)

        def app(_environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
            return [b"ok"]

        worker.wsgi = app
        gunicorn_child.post_worker_init(worker)

        def serve() -> threading.Thread:
            """What the worker's main loop does with a new connection: handle() on a request thread."""
            sock, addr = listener.accept()
            conn = TConn(cfg, sock, addr, listener.getsockname())
            thread = threading.Thread(target=worker.handle, args=(conn,), daemon=True)
            thread.start()
            return thread

        return listener.getsockname()[1], serve

    yield start
    listener.close()
    for worker in workers:
        worker.tmp.close()


def test_lw2_2_gunicorn_drops_a_client_that_trickles_its_headers(gthread):
    port, serve = gthread()
    with _connect(port) as client:
        thread = serve()
        elapsed = _trickle(client, lambda: (thread.join(0.2), thread.is_alive())[1])
        assert not thread.is_alive(), "the request thread is still reading the headers"
        assert elapsed < 3


def test_lw2_2_gunicorn_caps_the_slow_header_readers_of_one_address(gthread):
    port, serve = gthread(header_timeout=30)
    slow = []
    try:
        for _ in range(2):
            client = _connect(port, "127.0.0.2")
            slow.append((client, serve()))
            client.sendall(b"GET / HTTP/1.1\r\n")
        time.sleep(0.1)
        for _thread in (thread for _client, thread in slow):
            assert _thread.is_alive()
        with _connect(port, "127.0.0.2") as extra:
            thread = serve()
            extra.sendall(b"GET / HTTP/1.1\r\n")
            thread.join(1)
            assert not thread.is_alive(), "a third slow connection from one address took a thread"
        # Another address is served at once.
        with _connect(port) as client:
            thread = serve()
            client.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            assert client.recv(100).startswith(b"HTTP/1.1 200")
    finally:
        for client, thread in slow:
            client.close()
            thread.join(2)


def test_lw2_2_gunicorn_serves_ordinary_requests(gthread):
    port, serve = gthread()
    with _connect(port) as client:
        serve()
        client.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
        time.sleep(0.2)  # within the deadline
        client.sendall(b"\r\n")
        assert client.recv(100).startswith(b"HTTP/1.1 200")


# KW2-3 follow-up (the flaky test_kw2_3_one_wikis_token_cannot_shut_the_other_wikis_out): a wiki whose requests are
# refused for its busy cap pushed another wiki's new connection out, or overflowed the listen backlog


def test_kw2_3_crowded_connections_never_push_out_another_wikis_new_connection():
    module = load_server()
    speech = module.make_server(module.Settings(token="x" * 32, port=0), FakeEngine())  # 8 per address, 4 pending
    address = "127.0.0.1"
    evicted = []

    def connect(tenant: str) -> object:
        sock = object()  # what the server keeps of a connection
        claimed, evict = speech._claim(address, sock)
        assert claimed
        if evict is not None:
            evicted.append(evict)
        if tenant:
            speech.busy(address, sock, tenant, 2)  # what the handler does once it read the token
        return sock

    try:
        for _ in range(6):  # evil: 2 requests in progress, 4 refused (429, about to close)
            connect("evil")
        school = connect("")  # school's new connection: its request has not been read yet
        for _ in range(8):  # evil reconnects at once after every 429
            connect("evil")
        assert school not in evicted, "evil's refused requests closed school's connection"
        assert speech._per_client[address][school] == "pending"
    finally:
        speech.server_close()


def test_kw2_3_connection_attempts_are_not_dropped_by_a_short_listen_backlog():
    module = load_server()
    speech = module.make_server(module.Settings(token="x" * 32, port=0), FakeEngine())  # nothing accepts
    clients = []
    try:
        for _ in range(32):
            # The kernel drops a connection attempt over the backlog; its client retries only after a second.
            clients.append(socket.create_connection(speech.server_address, timeout=0.5))
    finally:
        for sock in clients:
            sock.close()
        speech.server_close()
