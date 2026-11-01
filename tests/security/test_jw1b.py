"""Regression tests for JW1-1, JW1-2, KW2-1, KW2-2 and KW2-3 (full re-audit pass 5: download places, Gunicorn
connections, the desktop child's write timeout and the GPU speech server's fairness between wikis)."""

from __future__ import annotations

import contextlib
import http.client
import importlib
import json
import logging
import os
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tests.hosting.conftest import portal  # noqa: F401

REPO = Path(__file__).resolve().parents[2]

# JW1-2: small responses sent back to back on one connection, and connections the client never closes

GUNICORN_CONFIGS = {
    "wiki": lambda: vars(importlib.reload(importlib.import_module("bananawiki.ops.gunicorn_conf"))),
    "portal": lambda: runpy.run_path(str(REPO / "hosting" / "gunicorn.conf.py")),
    "desktop": lambda: runpy.run_path(str(REPO / "bananawiki" / "desktop" / "gunicorn_child.py")),
}


@pytest.fixture
def gunicorn_util(monkeypatch):
    util = pytest.importorskip("gunicorn.util")
    monkeypatch.setattr(util, "close_graceful", util.close_graceful)  # the configurations replace it; restored after
    return util


@pytest.mark.parametrize("name", sorted(GUNICORN_CONFIGS))
def test_jw1_2_gunicorn_closes_a_connection_without_waiting_for_the_client(gunicorn_util, name):
    settings = GUNICORN_CONFIGS[name]()
    with socket.create_server(("127.0.0.1", 0)) as listener:
        client = socket.create_connection(listener.getsockname())
        server, _ = listener.accept()
    with client:
        client.sendall(b"GET / HTTP/1.1\r\nHost: wiki\r\nConnection: close\r\n\r\n")  # and never closes
        time.sleep(0.05)
        started = time.monotonic()
        gunicorn_util.close_graceful(server)  # gthread calls it on the loop that accepts every connection
        assert time.monotonic() - started < 0.5, "a client that never closes stalls the worker's main loop"
        client.settimeout(2)
        assert client.recv(10) == b"", "the client still sees the end of the answer"
    assert settings["keepalive"] == 0, "one request per connection"


PIPELINE_APP = """
def app(environ, start_response):
    body = b"x" * 30000 if environ["PATH_INFO"] == "/login" else b"ok"
    start_response("200 OK", [("Content-Type", "text/html"), ("Content-Length", str(len(body)))])
    return [body]
"""


@contextlib.contextmanager
def _gunicorn(app_source: str, settings: str):
    """A real Gunicorn serving *app_source* on a Unix socket, with *settings* (Python) as its configuration."""
    pytest.importorskip("gunicorn")
    folder = Path(tempfile.mkdtemp(prefix="bw-jw1b-"))  # short: AF_UNIX paths are limited to 108 bytes
    (folder / "jw1b_app.py").write_text(app_source)
    (folder / "settings.py").write_text(settings)
    address = str(folder / "w.sock")
    process = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "-c", str(folder / "settings.py"), "-b", f"unix:{address}",
         "--chdir", str(folder), "jw1b_app:app"],
        env={**os.environ, "PYTHONPATH": str(REPO), "BW_ACCESS_LOG": "off"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not os.path.exists(address):
            assert process.poll() is None and time.monotonic() < deadline, "Gunicorn did not start"
            time.sleep(0.05)
        yield address
    finally:
        process.terminate()
        try:
            process.wait(30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        shutil.rmtree(folder, ignore_errors=True)


def _unix(address: str, receive_buffer: int = 0) -> socket.socket:
    deadline = time.monotonic() + 30
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if receive_buffer:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        try:
            sock.connect(address)
            return sock
        except OSError:
            sock.close()
            assert time.monotonic() < deadline
            time.sleep(0.05)


@pytest.mark.skipif(os.name != "posix", reason="Gunicorn runs on POSIX systems")
def test_jw1_2_requests_sent_back_to_back_do_not_hold_the_threads():
    settings = "from bananawiki.ops.gunicorn_conf import keepalive, worker_class\nworkers = 1\nthreads = 2\n"
    with _gunicorn(PIPELINE_APP, settings) as address:
        _unix(address).close()  # started
        attackers = [_unix(address, receive_buffer=2048) for _ in range(4)]
        # 16 pages of 30 KB on each connection, each small enough to need no download place, never read.
        for _ in range(16):
            for sock in attackers:
                with contextlib.suppress(OSError):  # the server may have closed it after the first answer
                    sock.sendall(b"GET /login HTTP/1.1\r\nHost: wiki\r\n\r\n")
            time.sleep(0.03)
        time.sleep(0.3)
        probe = _unix(address)
        probe.sendall(b"GET /health HTTP/1.1\r\nHost: wiki\r\nConnection: close\r\n\r\n")
        probe.settimeout(3)
        try:
            answer = probe.recv(64)
        except TimeoutError:
            answer = b""
        probe.close()
        for sock in attackers:
            sock.close()
    assert answer.startswith(b"HTTP/1.1 200"), "every thread is held writing small answers nobody reads"


# KW2-2: the desktop launcher's gunicorn child gives up on a client that stops reading


def test_kw2_2_the_desktop_gunicorn_child_installs_the_write_timeout(gunicorn_util):
    from types import SimpleNamespace

    settings = GUNICORN_CONFIGS["desktop"]()
    worker = SimpleNamespace(wsgi=lambda environ, start_response: [b"x"])
    settings["post_worker_init"](worker)  # KeyError without the hook: writes would wait for the client forever
    served, client = socket.socketpair()
    with served, client:
        worker.wsgi({"gunicorn.socket": served}, None)
        assert served.gettimeout() == 300, "a write that waits 300 s for room ends the response"


# JW1-1 / KW2-1: one client holds a quarter of the download places, and exports take theirs before the work


def _big_upload(app, db) -> None:
    from bananawiki.core import downloads

    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    folder = Path(app.config["BW"].folders.uploads)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "big.png").write_bytes(b"x" * (downloads.LARGE_BODY + 1))


def test_jw1_1_it_takes_four_clients_to_fill_the_places(app, app_factory, db, make_user, login):
    _big_upload(app, db)
    # The places a production wiki builds (BW_DOWNLOAD_THREADS, 8): test apps leave them off.
    places = app.extensions["bananawiki.downloads"] = app_factory(testing=False).extensions["bananawiki.downloads"]
    mallory = make_user("mallory")
    signed_in = app.test_client()
    login(signed_in, mallory)

    def get(client):
        return client.get("/static/uploads/big.png")  # kept open: a slow reader

    def anonymous(addr: str):
        client = app.test_client()
        client.environ_base["REMOTE_ADDR"] = addr
        return client

    # The finding: mallory's account and mallory's own address without the cookie took all 8 places.
    held = [get(signed_in), get(signed_in), get(anonymous("127.0.0.2")), get(anonymous("127.0.0.2"))]
    assert [r.status_code for r in held] == [200] * 4
    assert get(signed_in).status_code == 429 and get(anonymous("127.0.0.2")).status_code == 429
    assert places.active == 4, "one person (an account and its own network) holds half at most"
    victim = get(anonymous("198.51.100.7"))
    assert victim.status_code == 200, "everyone else still gets downloads"
    victim.close()
    held += [get(anonymous("203.0.113.1")), get(anonymous("203.0.113.1")),
             get(anonymous("192.0.2.1")), get(anonymous("192.0.2.1"))]
    assert get(anonymous("198.51.100.7")).status_code == 503 and places.active == 8, "four clients fill it"
    for response in held:
        response.close()
    assert places.active == 0


def test_jw1_1_kw2_1_every_server_gives_four_clients_their_share():
    from bananawiki.core import downloads
    from bananawiki.desktop import server
    from bananawiki.hosting.runtime.agent import TENANT_DOWNLOAD_THREADS
    from bananawiki.ops.tenant_entrypoint import web_command

    assert downloads.Places(8).per_client == 2 and downloads.Places(4).per_client == 1
    # Portal (one place each), tenant and desktop (two each): four clients reading slowly to fill a process.
    for total in (downloads.PORTAL_PLACES, TENANT_DOWNLOAD_THREADS):
        assert total // downloads.Places(total).per_client >= 4
    assert TENANT_DOWNLOAD_THREADS == 8, "two places per client, as a standalone wiki"
    command = web_command({"BW_DOWNLOAD_THREADS": str(TENANT_DOWNLOAD_THREADS)})
    assert command[command.index("--threads") + 1] == "12", "the tenant runs a thread per place"
    settings = runpy.run_path(str(REPO / "hosting" / "gunicorn.conf.py"))
    from bananawiki.hosting import wikihosts

    # Plus a thread per request waiting for a proxied wiki (audit NW4-2): the places keep theirs.
    assert settings["threads"] == 4 + downloads.PORTAL_PLACES + wikihosts.WAITING_PLACES == 12
    source = (REPO / "bananawiki" / "desktop" / "datafolder.py").read_text()
    assert '"BW_DOWNLOAD_THREADS": "8"' in source and server.THREADS - 8 == 4, "the desktop keeps 4 for pages"


def test_jw1_1_a_refused_export_is_not_built_counted_or_audited(app, db, make_user, login, admin_client,
                                                                  monkeypatch):
    from bananawiki.core import downloads
    from bananawiki.wiki.features.user_data_export import routes, service

    places = app.extensions["bananawiki.downloads"] = downloads.Places(1)
    built: list[str] = []
    stream = service.stream
    monkeypatch.setattr(service, "stream", lambda user: built.append(user["username"]) or stream(user))
    bob = make_user("bob")
    release, _ = places.take("network:192.0.2.9")  # a slow reader elsewhere holds the process's only place
    for _ in range(routes.EXPORTS_PER_HOUR + 2):
        refused = admin_client.get(f"/admin/users/{bob['id']}/export")
        assert refused.status_code == 503 and refused.headers["Retry-After"] == "5"
    assert built == [], "refused before the archive is built"
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'user.data_exported'") == 0
    release()
    for _ in range(routes.EXPORTS_PER_HOUR):  # none of the refused ones counted against the limit
        answer = admin_client.get(f"/admin/users/{bob['id']}/export", buffered=True)
        assert answer.status_code == 200 and answer.data[:2] == b"PK"
    assert places.active == 0, "each export kept the place it took first until it was closed"
    assert len(built) == routes.EXPORTS_PER_HOUR


def test_jw1_1_a_place_taken_first_is_given_back_without_a_large_response(app, admin_client, monkeypatch):
    from bananawiki.core import downloads
    from bananawiki.wiki.features.user_data_export import service

    places = app.extensions["bananawiki.downloads"] = downloads.Places(4)
    assert admin_client.get("/admin/users/nobody/export").status_code == 404 and places.active == 0
    kept = admin_client.get("/settings/export")  # open: the stream keeps it
    assert kept.status_code == 200 and places.active == 1
    kept.close()
    assert places.active == 0

    def broken(user):
        raise RuntimeError("archive failed")

    monkeypatch.setattr(service, "stream", broken)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    assert admin_client.get("/settings/export").status_code == 500 and places.active == 0


EXPORTS = {
    "wiki": ["user_data_export.own_export", "user_data_export.admin_export", "site_admin.migration_export",
             "page_export.export_pdf", "page_export.export_history_pdf", "page_export.bulk_export",
             "attachments.download_all", "chat.dm_export", "chat.group_export", "kanban.export",
             "canvas.export"],
    "portal": ["dashboard.download", "admin.download_instance", "admin.export_platform"],
}


def test_jw1_1_exports_take_their_place_before_the_work(app, portal):  # noqa: F811
    for application, name in ((app, "wiki"), (portal, "portal")):
        for endpoint in EXPORTS[name]:
            assert getattr(application.view_functions[endpoint], "download_place_first", False), endpoint


def test_jw1_1_the_portal_refuses_an_export_before_doing_it(portal):  # noqa: F811
    from flask import Response

    from bananawiki.core import downloads
    from bananawiki.hosting import auth

    built: list[int] = []

    @downloads.place_first
    def export():
        built.append(1)
        return Response(b"x" * (downloads.LARGE_BODY + 1))

    portal.add_url_rule("/jw1b-export", "jw1b_export", auth.public(export))
    places = portal.extensions["bananawiki.hosting.downloads"] = downloads.Places(downloads.PORTAL_PLACES)
    holders = [places.take(f"network:192.0.2.{n}")[0] for n in range(downloads.PORTAL_PLACES)]
    refused = portal.test_client().get("/jw1b-export")
    assert refused.status_code == 503 and built == []
    holders.pop()()
    answer = portal.test_client().get("/jw1b-export")
    assert answer.status_code == 200 and built == [1] and places.active == downloads.PORTAL_PLACES
    answer.close()
    for release in holders:
        release()
    assert places.active == 0


# KW2-3: one hosted wiki's own speech token keeps the shared address's connections and the GPU to itself


class _SlowEngine:
    """Synthesis that takes a while, as a long page does on a real GPU."""

    def synthesize(self, text, language):
        time.sleep(0.1)
        return b"ID3" + b"\x01" * 300, "audio/mpeg"

    def details(self):
        return {}


def _synthesize(conn, token: str) -> int:
    conn.request("POST", "/synthesize", body=json.dumps({"text": "x" * 100}),
                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    response = conn.getresponse()
    response.read()
    return response.status


def test_kw2_3_one_wikis_token_cannot_shut_the_other_wikis_out(caplog):
    from tests.features.test_tts_gpu import load_server

    caplog.set_level(logging.INFO, logger="bananawiki-tts-gpu")
    module = load_server()
    master = "x" * 32
    server = module.make_server(module.Settings(token=master, port=0), _SlowEngine())  # default limits
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    evil, school = module.tenant_token(master, "evil"), module.tenant_token(master, "school")
    stop = threading.Event()

    def hog():
        # As in the finding: 8 keep-alive connections (TTS_MAX_CONNECTIONS_PER_CLIENT), each sending the next
        # request as soon as the last one was answered.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        while not stop.is_set():
            try:
                _synthesize(conn, evil)
            except (OSError, http.client.HTTPException):
                conn.close()
        conn.close()

    hogs = [threading.Thread(target=hog, daemon=True) for _ in range(8)]
    try:
        for thread in hogs:
            thread.start()
        time.sleep(0.3)
        outcomes = []
        for _ in range(5):
            started = time.monotonic()
            try:
                outcomes.append(_synthesize(http.client.HTTPConnection("127.0.0.1", port, timeout=10), school))
            except (OSError, http.client.HTTPException) as error:
                outcomes.append(type(error).__name__)
            outcomes.append(time.monotonic() - started < 1.0)  # waits for a synthesis or two, not evil's queue
    finally:
        stop.set()
        for thread in hogs:
            thread.join(5)
        server.shutdown()
        server.server_close()
    assert outcomes == [200, True] * 5, outcomes
    logged = caplog.text
    assert "tenant=school" in logged and "tenant=evil" in logged, "the log names the wiki of each request"
    assert "Refused a request of tenant evil" in logged and evil.split(".")[2] not in logged, "never the token"


def test_kw2_3_a_free_gpu_slot_goes_to_the_wiki_served_longest_ago():
    from tests.features.test_tts_gpu import load_server

    slots = load_server()._FairSlots(1)
    assert slots.acquire("evil", 1)  # synthesising
    order: list[str] = []

    def wait(client: str) -> None:
        if slots.acquire(client, 5):
            order.append(client)
            slots.release()

    waiting = []
    for client in ("evil", "evil", "school"):  # evil queued first, twice
        waiting.append(threading.Thread(target=wait, args=(client,)))
        waiting[-1].start()
        time.sleep(0.05)
    slots.release()
    for thread in waiting:
        thread.join(5)
    assert order == ["school", "evil", "evil"]
    assert slots.acquire("operator", 0), "free again"
