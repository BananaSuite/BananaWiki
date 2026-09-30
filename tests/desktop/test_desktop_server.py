"""Starting and stopping the wiki for a data folder (real servers on free loopback ports)."""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import urllib.request

import pytest

from bananawiki.desktop import DesktopError, network, server
from bananawiki.desktop.datafolder import DataFolder
from bananawiki.desktop.server import WikiServer


def fetch(url: str) -> int:
    return server._get(url, timeout=10)


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
        assert info.setup_token == hmac.new(folder.secret_key().encode(), b"initial-admin-setup",
                                            hashlib.sha256).hexdigest()
    finally:
        wiki.stop()
    assert not port_open(free_port)
