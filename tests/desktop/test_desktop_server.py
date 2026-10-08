"""Starting and stopping the wiki for a data folder (real servers on free loopback ports)."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import os
import socket
import sys
import threading
import urllib.request

import pytest

from bananawiki.desktop import DesktopError, network, server
from bananawiki.desktop.datafolder import DataFolder
from bananawiki.desktop.server import WikiServer


def fetch(url: str) -> int:
    return server._get(url, timeout=10)[0]


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def running(folder, free_port):
    wiki = WikiServer(folder, port=free_port, backend="waitress")
    info = wiki.start()
    yield wiki, info
    wiki.stop()


def test_waitress_server_serves_the_folder_on_loopback(running, folder, free_port):
    wiki, info = running
    assert wiki.running
    assert info.host == network.LOOPBACK_HOST and not info.shared and info.lan_url is None
    assert info.port == free_port and info.backend == "waitress"
    assert info.local_url == f"http://127.0.0.1:{free_port}/"
    assert fetch(info.local_url + "health") == 200
    assert fetch(info.local_url + "setup") == 200
    assert folder.database.is_file()


def test_setup_code_is_derived_from_the_folder_key(running, folder):
    _wiki, info = running
    expected = hmac.new(folder.secret_key().encode(), b"initial-admin-setup", hashlib.sha256).hexdigest()
    assert info.setup_token == expected


def test_stop_closes_the_port_and_releases_the_folder(folder, free_port):
    wiki = WikiServer(folder, port=free_port, backend="waitress")
    info = wiki.start()
    keep_alive = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    keep_alive.open(info.local_url + "health").read()
    wiki.stop()
    assert not wiki.running and wiki.info is None
    assert not port_open(free_port)
    with folder.lock():
        pass
    wiki.stop()  # stopping twice is harmless


def test_the_same_folder_cannot_be_served_twice(running, folder):
    other = WikiServer(DataFolder(folder.root), backend="waitress")
    with pytest.raises(DesktopError) as caught:
        other.start()
    assert caught.value.key == "folder_in_use"


def test_start_twice_is_refused(running):
    wiki, _info = running
    with pytest.raises(DesktopError) as caught:
        wiki.start()
    assert caught.value.key == "already_running"


def test_restart_after_stop(folder, free_port):
    wiki = WikiServer(folder, port=free_port, backend="waitress")
    wiki.start()
    wiki.stop()
    info = wiki.start()
    try:
        assert fetch(info.local_url + "health") == 200
    finally:
        wiki.stop()


def test_health_answers_carry_the_start_nonce(folder, free_port):
    wiki = WikiServer(folder, port=free_port, backend="waitress")
    first = wiki.start()
    nonce = server._get(first.local_url + "health", timeout=10)[1].get(server.START_HEADER)
    assert nonce and server._get(first.local_url + "setup", timeout=10)[1].get(server.START_HEADER) is None
    wiki.stop()
    second = wiki.start()
    try:
        assert server._get(second.local_url + "health", timeout=10)[1].get(server.START_HEADER) not in (None, nonce)
    finally:
        wiki.stop()


class _Alive:
    port = 0

    def alive(self) -> bool:
        return True


@pytest.mark.parametrize("status, nonce", [(200, None), (200, "guessed"), (503, None)])
def test_another_server_on_the_port_is_not_taken_for_the_wiki(status, nonce):
    class Foreign(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(status)
            if nonce:
                self.send_header(server.START_HEADER, nonce)
            self.end_headers()

        def log_message(self, *args):
            pass

    foreign = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Foreign)
    threading.Thread(target=foreign.serve_forever, daemon=True).start()
    try:
        with pytest.raises(DesktopError) as caught:
            server.wait_until_ready(foreign.server_address[1], _Alive(), timeout=10, nonce="the-real-one")
    finally:
        foreign.shutdown()
        foreign.server_close()
    assert caught.value.key == "port_taken"


def test_two_wikis_never_share_a_port(folder, tmp_path, free_port, monkeypatch):
    monkeypatch.setattr(network, "FALLBACK_PORTS", (free_port + 1, free_port + 2))
    first = WikiServer(folder, port=free_port, backend="waitress")
    second = WikiServer(DataFolder(tmp_path / "other"), port=free_port, backend="waitress")
    first.start()
    try:
        assert second.start().port in (free_port + 1, free_port + 2)
        second.stop()
    finally:
        first.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="SO_EXCLUSIVEADDRUSE is Windows-only")
@pytest.mark.parametrize("share_on_lan", [False, True])
def test_no_other_program_can_bind_the_wiki_port_on_windows(folder, free_port, share_on_lan):
    wiki = WikiServer(folder, share_on_lan=share_on_lan, port=free_port, backend="waitress")
    wiki.start()
    try:
        for option in (socket.SO_REUSEADDR, socket.SO_EXCLUSIVEADDRUSE):
            with socket.socket() as intruder:
                intruder.setsockopt(socket.SOL_SOCKET, option, 1)
                with pytest.raises(OSError):
                    intruder.bind(("127.0.0.1", free_port))
    finally:
        wiki.stop()


def test_busy_port_falls_back_to_another(folder, free_port, monkeypatch):
    monkeypatch.setattr(network, "FALLBACK_PORTS", (free_port + 1, free_port + 2))
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", free_port))
        blocker.listen()
        wiki = WikiServer(folder, port=free_port, backend="waitress")
        try:
            info = wiki.start()
            assert info.port in (free_port + 1, free_port + 2)
        finally:
            wiki.stop()


def test_lan_binding_only_when_the_user_opted_in(folder, free_port, monkeypatch):
    monkeypatch.setattr(network, "lan_address", lambda: "192.168.1.20")
    assert WikiServer(folder)._host() == "127.0.0.1"
    wiki = WikiServer(folder, share_on_lan=True, port=free_port, backend="waitress")
    info = wiki.start()
    try:
        assert info.host == "0.0.0.0" and info.shared
        assert info.lan_url == f"http://192.168.1.20:{free_port}/"
        assert fetch(info.local_url + "health") == 200
    finally:
        wiki.stop()


def test_the_wiki_environment_does_not_leak_into_the_process(running, folder):
    assert os.environ.get("BW_INSTANCE_DIR") != str(folder.instance)
    assert os.environ.get("BW_EASY_DEPLOYMENT") is None


def test_a_broken_database_is_reported(folder, free_port):
    folder.prepare()
    folder.database.write_bytes(b"not a database" * 100)
    wiki = WikiServer(folder, port=free_port, backend="waitress")
    with pytest.raises(DesktopError) as caught:
        wiki.start()
    assert caught.value.key == "server_failed"
    with folder.lock():  # the failed start released the folder
        pass


def test_unknown_backend_is_refused(folder):
    with pytest.raises(DesktopError) as caught:
        WikiServer(folder, backend="uwsgi").start()
    assert caught.value.key == "no_server_backend"


@pytest.mark.skipif("gunicorn" not in server.available_backends(), reason="gunicorn is POSIX-only")
def test_gunicorn_backend_runs_a_child_process(folder, free_port):
    wiki = WikiServer(folder, port=free_port, backend="gunicorn")
    info = wiki.start()
    try:
        assert info.backend == "gunicorn"
        assert fetch(info.local_url + "health") == 200
        assert server._get(info.local_url + "health", timeout=10)[1].get(server.START_HEADER)
        assert info.setup_token == hmac.new(folder.secret_key().encode(), b"initial-admin-setup",
                                            hashlib.sha256).hexdigest()
    finally:
        wiki.stop()
    assert not port_open(free_port)
