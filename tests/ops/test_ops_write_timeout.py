# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""A client that stops reading a download no longer holds a Gunicorn thread forever (R-27)."""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from bananawiki.ops.write_timeout import FLOOR, GROWN_FLOOR, MAX_WRITE, UNSENT, guard

REPO = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024
SIZE = 1024 * MIB  # of STALL_APP's responses


def _download(_environ, start_response):
    start_response("200 OK", [("Content-Type", "application/octet-stream")])
    return (b"x" * MIB for _ in range(64))


class _Recorder:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


def test_a_client_that_stops_reading_frees_the_writing_thread():
    """What a gthread worker does with a response: sendall each piece, here to a peer that never reads."""
    server, client = socket.socketpair()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
    outcome = []

    def write():
        try:
            for piece in guard(_download, 0.5)({"gunicorn.socket": server}, lambda *_args: None):
                server.sendall(piece)
            outcome.append("finished")
        except OSError as error:
            outcome.append(error)

    thread = threading.Thread(target=write, daemon=True)
    try:
        thread.start()
        thread.join(15)
        assert not thread.is_alive(), "still blocked in sendall"
        assert isinstance(outcome[0], TimeoutError)
    finally:
        server.close()
        client.close()
        thread.join(5)


def test_long_pieces_are_written_in_steps_and_files_are_left_to_sendfile():
    class FileWrapper:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    sock = _Recorder()
    body = [b"a" * (3 * MAX_WRITE + 10), b"b" * 5]
    pieces = guard(lambda _e, _s: body, 30)({"gunicorn.socket": sock, "wsgi.file_wrapper": FileWrapper}, None)
    written = list(pieces)
    assert [len(piece) for piece in written] == [MAX_WRITE, MAX_WRITE, MAX_WRITE, 10, 5]
    assert b"".join(written) == b"".join(body) and sock.timeouts == [30]
    document = FileWrapper()
    sent = guard(lambda _e, _s: document, 30)({"gunicorn.socket": sock, "wsgi.file_wrapper": FileWrapper}, None)
    assert sent is document, "Gunicorn uses sendfile only for the file wrapper itself"
    assert sock.timeouts == [30, 30]
    streamed = FileWrapper()
    guard(lambda _e, _s: streamed, 30)({"gunicorn.socket": sock}, None).close()
    assert streamed.closed, "close() reaches the application's iterable even when nothing was read"


def test_without_gunicorn_or_with_no_limit_nothing_changes():
    body = [b"x"]
    assert guard(lambda _e, _s: body, 30)({}, None) is body
    assert guard(_download, 0) is _download


def test_http2_streams_are_left_alone():
    """An HTTP/2 stream shares its connection: a timeout would also close the idle connection."""
    sock, body = _Recorder(), [b"x"]
    environ = {"gunicorn.socket": sock, "gunicorn.http2.send_trailers": None, "HTTP_VERSION": "2"}
    assert guard(lambda _e, _s: body, 30)(environ, None) is body and sock.timeouts == []
    spoofed = {"gunicorn.socket": sock, "HTTP_VERSION": "2"}  # an HTTP/1.1 request with a "Version: 2" header
    list(guard(lambda _e, _s: body, 30)(spoofed, None))
    assert sock.timeouts == [30]


@pytest.mark.skipif(not hasattr(socket, "TCP_NOTSENT_LOWAT"), reason="Linux and macOS")
def test_tcp_connections_queue_little_unsent_data():
    """sendfile would otherwise fill megabytes of send buffer, a third of which must drain per period."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        client = socket.create_connection(listener.getsockname())
        server, _address = listener.accept()
        with client, server:
            guard(lambda _e, _s: [b"x"], 30)({"gunicorn.socket": server}, None)
            assert server.gettimeout() == 30
            assert server.getsockopt(socket.IPPROTO_TCP, socket.TCP_NOTSENT_LOWAT) == UNSENT


@pytest.mark.skipif(not hasattr(socket, "TCP_CORK"), reason="Linux")
def test_generated_bodies_leave_no_small_segments():
    """With TCP_NODELAY every unaligned write would end in a small segment, and a receive buffer at its
    maximum runs out of memory on those, drops data and stalls the retransmissions as well."""

    class FileWrapper:
        pass

    def corked(sock):
        return sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_CORK)

    with socket.create_server(("127.0.0.1", 0)) as listener:
        client = socket.create_connection(listener.getsockname())
        server, _address = listener.accept()
        with client, server:
            environ = {"gunicorn.socket": server, "wsgi.file_wrapper": FileWrapper}
            pieces = guard(lambda _e, _s: [b"a" * (2 * MAX_WRITE), b"b"], 30)(environ, None)
            assert corked(server)
            assert len(list(pieces)) == 3 and not corked(server), "the end of the body is sent at once"
            stopped = guard(lambda _e, _s: [b"a", b"b"], 30)(environ, None)
            next(iter(stopped))
            assert corked(server)
            stopped.close()
            assert not corked(server), "nor held when Gunicorn stops early"
            guard(lambda _e, _s: FileWrapper(), 30)(environ, None)
            assert not corked(server), "sendfile sends full segments"


STALL_APP = """
import os


def app(environ, start_response):
    if environ["PATH_INFO"] == "/file":  # an attachment or a whole-site export: sendfile
        path = os.path.join(os.path.dirname(__file__), "large.bin")
        start_response("200 OK", [("Content-Type", "application/octet-stream"),
                                  ("Content-Length", str(os.path.getsize(path)))])
        return environ["wsgi.file_wrapper"](open(path, "rb"))
    start_response("200 OK", [("Content-Type", "application/octet-stream")])
    return (b"x" * 1048576 for _ in range(1024))  # the personal data export: generated chunks
"""


@contextlib.contextmanager
def _gunicorn(bind, timeout):
    """The real worker serving STALL_APP with the hook from the wiki's Gunicorn settings; its log on exit."""
    pytest.importorskip("gunicorn")
    folder = Path(tempfile.mkdtemp(prefix="bw-stall-"))  # short: AF_UNIX paths are limited to 108 bytes
    (folder / "stall_app.py").write_text(STALL_APP)
    with open(folder / "large.bin", "wb") as large:
        large.truncate(SIZE)
    (folder / "settings.py").write_text(
        "from bananawiki.ops.gunicorn_conf import post_worker_init, worker_class\n"
        "workers = 1\nthreads = 2\n"
    )
    environ = {**os.environ, "PYTHONPATH": str(REPO), "BW_WRITE_TIMEOUT": str(timeout)}
    server = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "-c", str(folder / "settings.py"), "-b", bind(folder),
         "--chdir", str(folder), "stall_app:app"],
        env=environ, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    log = bytearray()
    try:
        yield folder, server, log
    finally:
        server.terminate()
        try:
            _out, errors = server.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            _out, errors = server.communicate()
        log += errors
        shutil.rmtree(folder, ignore_errors=True)


def _connect(server, family, address, receive_buffer=0):
    deadline = time.monotonic() + 30
    while True:
        client = socket.socket(family, socket.SOCK_STREAM)
        if receive_buffer:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        try:
            client.connect(address)
            return client
        except OSError:
            client.close()
            assert server.poll() is None and time.monotonic() < deadline, "Gunicorn did not start"
            time.sleep(0.1)


def _get(client, path):
    client.sendall(f"GET {path} HTTP/1.1\r\nHost: wiki\r\nConnection: close\r\n\r\n".encode())


@pytest.mark.skipif(os.name != "posix", reason="Gunicorn runs on POSIX systems")
@pytest.mark.parametrize("path", ["/stream", "/file"])
def test_gunicorn_drops_a_stalled_download(path):
    with _gunicorn(lambda folder: f"unix:{folder / 'wiki.sock'}", 1) as (folder, server, log):
        client = _connect(server, socket.AF_UNIX, str(folder / "wiki.sock"), receive_buffer=64 * 1024)
        _get(client, path)
        time.sleep(4)  # reads nothing: the worker's writes stall
        client.settimeout(15)
        received = 0
        while chunk := client.recv(MIB):
            received += len(chunk)
        client.close()
        assert received < 64 * MIB, "the stalled response was sent in full: no write timeout"
    assert b"timed out" in log


def _download_at_full_speed(client):
    """GET /file in full on a connection that stays open: meanwhile the kernel grows its receive buffer."""
    client.sendall(b"GET /file HTTP/1.1\r\nHost: wiki\r\n\r\n")
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(64 * 1024)
        assert chunk, "the connection was closed"
        data += chunk
    body = len(data) - data.index(b"\r\n\r\n") - 4
    while body < SIZE:
        chunk = client.recv(MIB)
        assert chunk, "the connection was closed"
        body += len(chunk)


@pytest.mark.skipif(not hasattr(socket, "TCP_NOTSENT_LOWAT"), reason="Linux and macOS")
@pytest.mark.parametrize(("fast", "per_period"), [(False, FLOOR), (True, GROWN_FLOOR)],
                         ids=["slow-from-the-start", "after-a-fast-download"])
@pytest.mark.parametrize("path", ["/stream", "/file"])
def test_gunicorn_keeps_a_slow_client_that_keeps_reading(path, fast, per_period):
    """The documented bytes per period keep a reader connected: over TCP, where sendfile would fill megabytes
    of send buffer, and after fast transfers, which grow the receive buffer that the kernel reopens in steps."""
    timeout = 2
    with socket.create_server(("127.0.0.1", 0)) as spare:
        port = spare.getsockname()[1]
    with _gunicorn(lambda _folder: f"127.0.0.1:{port}", timeout) as (_folder, server, log):
        client = _connect(server, socket.AF_INET, ("127.0.0.1", port))
        client.settimeout(30)
        if fast:  # like a proxy's pooled connection, or a client that slows down after a fast start
            _download_at_full_speed(client)
            grown = client.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            if grown <= 16 * FLOOR:  # some kernels grow it far less over loopback (GitHub's runners: about 1 MB)
                client.close()
                pytest.skip(f"the receive buffer grew to {grown} bytes only: no grown buffer to keep up with")
        _get(client, path)
        received, start = 0, time.monotonic()
        while time.monotonic() - start < 3 * timeout and (chunk := client.recv(32 * 1024)):
            received += len(chunk)  # paced until well after the buffers have filled up
            time.sleep(max(0.0, received * timeout / per_period - (time.monotonic() - start)))
        while chunk := client.recv(MIB):  # then the rest at full speed
            received += len(chunk)
        client.close()
    assert b"timed out" not in log
    assert received > SIZE, "the response was cut off"
