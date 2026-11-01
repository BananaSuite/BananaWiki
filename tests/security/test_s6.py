"""Regression tests for the SW5 findings of the full re-audit pass 13 (desktop waitress, tenant markers, speech)."""

from __future__ import annotations

import logging
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

from bananawiki.desktop import server
from bananawiki.ops.files import MAINTENANCE_MARKER, TENANT_DEPTH, untrusted_marker
from tests.features.test_tts_gpu import FakeEngine, load_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))  # as tests/ops imports its helpers
from test_ops_recovery import hosting as hosting  # noqa: E402  # type: ignore[import-not-found]

MIB = 1024 * 1024

# SW5-1: the desktop waitress server refuses a body it would only spool before the wiki can refuse it


class _App:
    extensions: dict = {}  # what _WaitressRunner.stop() looks into
    config = {"MAX_CONTENT_LENGTH": MIB}

    def __call__(self, environ, start_response):
        environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
        start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
        return [b"ok"]


@pytest.fixture
def waitress():
    runner = server._WaitressRunner(_App(), "127.0.0.1", [0], "nonce")
    yield runner
    runner.stop()


def _post(port: int, length: int, expect: bool = False) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Type: application/octet-stream\r\n"
                 + (b"Expect: 100-continue\r\n" if expect else b"") + b"Content-Length: %d\r\n\r\n" % length)
    return sock


def _status(sock: socket.socket) -> bytes:
    return sock.recv(200).split(b"\r\n", 1)[0]


def _until(condition, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_sw5_1_waitress_refuses_a_body_past_the_largest_upload_after_the_headers(waitress):
    sock = _post(waitress.port, MIB + 1)
    try:
        # No body byte was sent: waitress answers from the headers alone.
        assert _status(sock) == b"HTTP/1.1 413 Request Entity Too Large"
    finally:
        sock.close()
    sock = _post(waitress.port, MIB)
    try:
        sock.sendall(b"x" * MIB)
        assert _status(sock) == b"HTTP/1.1 200 OK"
    finally:
        sock.close()


def test_sw5_1_one_address_holds_at_most_its_body_budget(waitress):
    budget = waitress.server.body_budget
    assert budget == server.BODY_BUDGET_UPLOADS * MIB
    first, second = _post(waitress.port, MIB), _post(waitress.port, MIB)
    try:
        first.sendall(b"x" * 1000)
        second.sendall(b"x" * 1000)
        assert _until(lambda: waitress.server.body_bytes("127.0.0.1") == budget)
        third = _post(waitress.port, 1000)
        try:
            # Past the budget: refused right after the headers, before its body arrives.
            assert _status(third) == b"HTTP/1.1 413 Request Entity Too Large"
        finally:
            third.close()
        first.sendall(b"x" * (MIB - 1000))
        assert _status(first) == b"HTTP/1.1 200 OK"
        assert _until(lambda: waitress.server.body_bytes("127.0.0.1") == MIB)
        fourth = _post(waitress.port, 1000)
        try:
            fourth.sendall(b"x" * 1000)
            assert _status(fourth) == b"HTTP/1.1 200 OK"
        finally:
            fourth.close()
    finally:
        first.close()
        second.close()
    # A connection closed with its body half sent gives its budget back.
    assert _until(lambda: waitress.server.body_bytes("127.0.0.1") == 0)


def test_sw5_1_a_refused_request_expecting_100_continue_is_not_invited_to_send_its_body(waitress):
    budget = waitress.server.body_budget
    # Past the largest upload: waitress's own 413, not a "100 Continue" and then the body spooled up to the limit.
    sock = _post(waitress.port, MIB + 1, expect=True)
    try:
        assert _status(sock) == b"HTTP/1.1 413 Request Entity Too Large"
    finally:
        sock.close()
    first, second = _post(waitress.port, MIB, expect=True), _post(waitress.port, MIB, expect=True)
    try:
        assert _status(first) == _status(second) == b"HTTP/1.1 100 Continue"
        assert waitress.server.body_bytes("127.0.0.1") == budget
        third = _post(waitress.port, MIB, expect=True)
        try:
            # Past the budget: refused, not invited to send a body nobody counts.
            assert _status(third) == b"HTTP/1.1 413 Request Entity Too Large"
        finally:
            third.close()
        first.sendall(b"x" * MIB)
        assert _status(first) == b"HTTP/1.1 200 OK"
    finally:
        first.close()
        second.close()
    assert _until(lambda: waitress.server.body_bytes("127.0.0.1") == 0)


# SW5-2: a tenant's deeply nested maintenance-marker directory blocks every backup and update


def _nest(directory: Path, depth: int) -> None:
    """``directory/.banana-maintenance/x/x/...`` *depth* levels deep, by descriptor (deeper than PATH_MAX)."""
    (directory / MAINTENANCE_MARKER).mkdir()
    fd = os.open(directory / MAINTENANCE_MARKER, os.O_RDONLY | os.O_DIRECTORY)
    for _ in range(depth):
        os.mkdir("x", dir_fd=fd)
        child = os.open("x", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = child
    os.close(fd)


def test_sw5_2_a_deep_tree_under_the_marker_name_is_set_aside(tmp_path):
    tenant = tmp_path / "acme"
    tenant.mkdir()
    _nest(tenant, TENANT_DEPTH - 2)
    untrusted_marker(tenant, MAINTENANCE_MARKER, "on\n")  # within the bound: removed
    assert (tenant / MAINTENANCE_MARKER).read_text() == "on\n"
    assert sorted(os.listdir(tenant)) == [MAINTENANCE_MARKER]
    untrusted_marker(tenant, MAINTENANCE_MARKER, None)
    _nest(tenant, 1200)
    untrusted_marker(tenant, MAINTENANCE_MARKER, "on\n")
    assert (tenant / MAINTENANCE_MARKER).read_text() == "on\n"
    (aside,) = [name for name in os.listdir(tenant) if name != MAINTENANCE_MARKER]
    assert aside.startswith(MAINTENANCE_MARKER + ".too-deep-")
    untrusted_marker(tenant, MAINTENANCE_MARKER, None)
    assert not (tenant / MAINTENANCE_MARKER).exists()


def test_sw5_2_a_marker_directory_that_cannot_be_listed_leaks_no_descriptor(tmp_path, monkeypatch):
    tenant = tmp_path / "acme"
    (tenant / MAINTENANCE_MARKER).mkdir(parents=True)
    listdir = os.listdir
    before = len(listdir("/proc/self/fd"))

    def refuse(path):
        raise PermissionError(13, "Permission denied")

    with monkeypatch.context() as patch:
        patch.setattr(os, "listdir", refuse)
        for _ in range(5):
            with pytest.raises(PermissionError):
                untrusted_marker(tenant, MAINTENANCE_MARKER, "on\n")
    assert len(listdir("/proc/self/fd")) == before


def test_sw5_2_a_deep_tenant_marker_tree_does_not_keep_the_platform_in_maintenance(hosting):
    root = hosting.root
    _nest(root / "data/instances/acme", 1200)
    for _ in range(2):
        assert hosting.manager.backup()
        assert not (root / "data" / MAINTENANCE_MARKER).exists()
        assert not (root / "config/transaction.json").exists()
        assert not (root / "data/instances/acme" / MAINTENANCE_MARKER).exists()


# SW5-3: a client resetting its connection makes the GPU speech server print a traceback


def test_sw5_3_speech_server_treats_a_reset_connection_as_a_client_gone(capsys, caplog):
    module = load_server()
    speech = module.make_server(module.Settings(token="x" * 32, port=0), FakeEngine())
    threading.Thread(target=speech.serve_forever, daemon=True).start()
    caplog.set_level(logging.INFO, logger="bananawiki-tts-gpu")
    try:
        port = speech.server_address[1]
        for partial in (b"", b"GET /hea", b"GET /health HTTP/1.1\r\nHost:"):
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            if partial:
                sock.sendall(partial)
            time.sleep(0.05)  # read by the handler before the reset arrives
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()  # a TCP reset
        time.sleep(0.2)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            assert sock.recv(200).startswith(b"HTTP/1.1 ")
    finally:
        speech.shutdown()
        speech.server_close()
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err and "Exception occurred" not in captured.err
    assert not [record for record in caplog.records if record.levelno > logging.INFO]
