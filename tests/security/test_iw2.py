"""Regression tests for the IW2 findings of the full re-audit pass 4 (hosting portal, ops, desktop)."""

from __future__ import annotations

import base64
import io
import logging
import socket

import pytest

from tests.hosting.conftest import login, make_account, make_wiki, portal, query, web  # noqa: F401
from tests.hosting.test_cross_wiki_sign_in import platform  # noqa: F401

# IW2-1: tenant-chosen names reach the root operator's terminal with their control characters escaped

OSC = "\x1b]52;c;ZWNobyBwd25lZAo=\x07\x1b]0;all good\x07"  # sets the clipboard, retitles the window


def test_iw2_1_a_tenant_folder_name_is_logged_escaped(tmp_path, caplog):
    from bananawiki.ops import profile, snapshot

    root = tmp_path / "inst"
    data = root / "data"
    path = data / "instances" / "acme" / OSC
    for _ in range(70):
        path = path / "d"
    path.mkdir(parents=True)
    (path / "f.txt").write_text("x")
    (root / "site").mkdir()
    (root / "config").mkdir()
    with caplog.at_level(logging.WARNING):
        snapshot.sources(root, lambda p: profile.inside_tenant(p, data))
    messages = [record.getMessage() for record in caplog.records]
    assert any("nested more than" in message for message in messages)
    assert not any("\x1b" in message or "\x07" in message for message in messages)
    assert any("\\x1b]52;c;" in message for message in messages)


def test_iw2_1_every_control_character_is_escaped():
    from bananawiki.core.logformat import one_line, printable

    assert one_line("a\x1b[2Kb\x9bc\x7fd‮e f\r\ng") == "a\\x1b[2Kb\\x9bc\\x7fd\\u202ee\\u2028f  g"
    assert printable("line\nnext\r") == "line\nnext\\x0d"
    assert printable("Ünïcödé wiki 🍌\tok") == "Ünïcödé wiki 🍌\tok"


def test_iw2_1_the_controller_and_the_portal_logs_escape_whatever_a_call_left_raw(capsys, monkeypatch):
    from bananawiki.core import logformat
    from bananawiki.ops import cli

    monkeypatch.setattr(logging, "lastResort", logging.lastResort)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    assert cli.main(["status"]) == 1
    capsys.readouterr()
    assert isinstance(logging.lastResort, logformat.StderrHandler)
    record = logging.LogRecord("bananawiki.ops", logging.WARNING, __file__, 1, "Leaving %s out", (OSC,), None)
    logging.lastResort.handle(record)
    err = capsys.readouterr().err
    assert err.startswith("Leaving \\x1b]52;c;") and "\x1b" not in err
    assert "\x1b" not in logformat.formatter("text").format(record)


# IW2-2: malformed and unauthenticated server-to-server OAuth requests are bounded per network

def _basic(client_id: str, secret: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()}


def _credentials(platform) -> tuple[str, str]:  # noqa: F811
    from bananawiki.hosting import oauth
    from bananawiki.hosting.db import connection_scope, db

    with platform["portal"].test_request_context("/"), connection_scope():
        client = oauth.client_for(db.one("SELECT * FROM instances WHERE id = ?", (platform["inst"]["id"],)))
    return client.client_id, client.client_secret


def _code(platform, account: dict, client_id: str) -> str:  # noqa: F811
    from bananawiki.hosting import oauth
    from bananawiki.hosting.db import connection_scope

    with platform["portal"].test_request_context("/"), connection_scope():
        return oauth.issue_code(client_id, account["id"], "https://team.example/cb", "", "")


def _client(platform, addr: str):  # noqa: F811
    client = platform["portal"].test_client()
    client.environ_base["REMOTE_ADDR"] = addr
    return client


def _form_parses(monkeypatch) -> list[int]:
    from werkzeug import formparser

    parses: list[int] = []
    original = formparser.FormDataParser.parse

    def counting(self, *args, **kwargs):
        parses.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(formparser.FormDataParser, "parse", counting)
    return parses


def test_iw2_2_malformed_token_requests_are_counted_and_then_refused_unread(platform, monkeypatch):  # noqa: F811
    from bananawiki.hosting import limits

    monkeypatch.setattr(limits, "BAD_CLIENT_PER_MINUTE", 3)
    parses = _form_parses(monkeypatch)
    attacker = _client(platform, "203.0.113.9")
    codes = [attacker.post("/oauth/token", data={"grant_type": "x", "f": (io.BytesIO(b"A" * 32_000), "a.bin")},
                           content_type="multipart/form-data").status_code for _ in range(6)]
    assert codes == [400, 400, 400, 429, 429, 429]
    assert len(parses) == 3, "a network over its budget is refused before its body is read"
    for form in ({"grant_type": "authorization_code", "code": "x"}, {}):  # no credentials, no grant type
        assert _client(platform, "203.0.113.10").post("/oauth/token", data=form).status_code == 400
    assert attacker.post("/oauth/link", json={"client_secret": "x"}).status_code == 429
    assert attacker.get("/oauth/signouts").status_code == 429
    # The bearer endpoints still serve a valid token, and the wiki's own address is not affected.
    client_id, secret = _credentials(platform)
    form = {"grant_type": "authorization_code", "code": _code(platform, platform["owner"], client_id),
            "redirect_uri": "https://team.example/cb"}
    answer = _client(platform, "10.0.0.5").post("/oauth/token", data=form, headers=_basic(client_id, secret))
    assert answer.status_code == 200
    token = answer.get_json()["access_token"]
    assert attacker.get("/oauth/userinfo", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_iw2_2_server_to_server_bodies_are_small(platform):  # noqa: F811
    client_id, secret = _credentials(platform)
    form = {"grant_type": "authorization_code", "code": "x" * 70_000}
    assert _client(platform, "10.0.0.5").post("/oauth/token", data=form,
                                              headers=_basic(client_id, secret)).status_code == 413


def test_iw2_2_bogus_bearers_over_the_limit_cost_a_read_only(platform, monkeypatch):  # noqa: F811
    from bananawiki.core import sqlite as core_sqlite
    from bananawiki.hosting import limits

    monkeypatch.setattr(limits, "BAD_TOKEN_PER_MINUTE", 3)
    client_id, secret = _credentials(platform)
    wiki = _client(platform, "10.0.0.5")
    form = {"grant_type": "authorization_code", "code": _code(platform, platform["owner"], client_id),
            "redirect_uri": "https://team.example/cb"}
    token = wiki.post("/oauth/token", data=form, headers=_basic(client_id, secret)).get_json()["access_token"]
    writes = {"transactions": 0, "inserts": 0}
    transaction, execute = core_sqlite.Session.transaction, core_sqlite.Session.execute

    def counting_transaction(self):
        writes["transactions"] += 1
        return transaction(self)

    def counting_execute(self, sql, params=()):
        writes["inserts"] += sql.lstrip().upper().startswith("INSERT")
        return execute(self, sql, params)

    monkeypatch.setattr(core_sqlite.Session, "transaction", counting_transaction)
    monkeypatch.setattr(core_sqlite.Session, "execute", counting_execute)
    codes = [wiki.get("/oauth/userinfo", headers={"Authorization": "Bearer nope"}).status_code for _ in range(20)]
    assert codes == [401] * 3 + [429] * 17
    assert writes == {"transactions": 0, "inserts": 3}
    assert wiki.get("/oauth/verify", headers={"Authorization": f"Bearer {token}"}).status_code == 200, \
        "a valid token is never refused for the failures of its network"


def test_iw2_2_codes_a_wiki_is_made_to_send_without_a_code_count_per_wiki(platform, monkeypatch):  # noqa: F811
    from bananawiki.hosting import limits

    monkeypatch.setattr(limits, "BAD_GRANT_PER_MINUTE", 3)
    monkeypatch.setattr(limits, "BAD_CLIENT_PER_MINUTE", 3)
    client_id, secret = _credentials(platform)
    wiki = _client(platform, "10.0.0.5")
    empty = {"grant_type": "authorization_code", "code": ""}
    codes = [wiki.post("/oauth/token", data=empty, headers=_basic(client_id, secret)).status_code for _ in range(5)]
    assert codes == [400, 400, 400, 429, 429]
    form = {"grant_type": "authorization_code", "code": _code(platform, platform["owner"], client_id),
            "redirect_uri": "https://team.example/cb"}
    assert wiki.post("/oauth/token", data=form, headers=_basic(client_id, secret)).status_code == 200, \
        "the wiki's address is not refused, and a valid code is still exchanged"


# IW2-3: the desktop launcher's gunicorn child never runs code from the data folder


def _handed_over_folder(tmp_path):
    import json

    from bananawiki.desktop.datafolder import DataFolder

    root = tmp_path / "handed-over"
    root.mkdir()
    (root / ".portable-data.json").write_text(json.dumps({"product": "BananaWiki", "schema": 1}))
    markers = tmp_path / "ran-conf", tmp_path / "ran-module"
    (root / "gunicorn.conf.py").write_text(f"import pathlib; pathlib.Path({str(markers[0])!r}).write_text('x')\n")
    (root / "gunicorn").mkdir()
    (root / "gunicorn" / "__init__.py").write_text("")
    (root / "gunicorn" / "__main__.py").write_text(f"import pathlib; pathlib.Path({str(markers[1])!r}).write_text('x')\n")
    return DataFolder(root), markers


def test_iw2_3_the_gunicorn_child_starts_outside_the_data_folder(tmp_path, monkeypatch):
    from bananawiki.desktop import server

    folder, _markers = _handed_over_folder(tmp_path)
    folder.prepare()
    started = {}

    class Child:
        def __init__(self, command, **kwargs):
            started.update(kwargs, command=command)

    monkeypatch.setattr(server.subprocess, "Popen", Child)
    runner = server._GunicornRunner(folder, {}, "127.0.0.1", 5999, "nonce")
    runner._log.close()
    command, cwd = started["command"], started["cwd"]
    assert command[1] == "-P", "the working directory is never on the child's sys.path"
    config = command[command.index("-c") + 1]
    imports = [line for line in server._GUNICORN_CONFIG.read_text().splitlines()
               if line.startswith(("import ", "from "))]
    assert config == str(server._GUNICORN_CONFIG) and all(line.startswith("from bananawiki.") for line in imports)
    assert not str(cwd).startswith(str(folder.root)) and command[command.index("--chdir") + 1] == cwd


def test_iw2_3_a_handed_over_folder_runs_no_code_of_its_own(tmp_path):
    from bananawiki.desktop import server

    if "gunicorn" not in server.available_backends():
        pytest.skip("gunicorn is POSIX-only")
    folder, markers = _handed_over_folder(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    wiki = server.WikiServer(folder, port=port, backend="gunicorn")
    try:
        info = wiki.start(timeout=60)
        assert server._get(info.local_url + "health", timeout=10)[0] == 200
    finally:
        wiki.stop()
    assert not any(marker.exists() for marker in markers)


# IW2-4: readers taking large responses slowly cannot hold every server thread


def test_iw2_4_large_responses_take_a_place_until_the_server_closes_them(app, db):
    from pathlib import Path

    from bananawiki.core import downloads

    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    folder = Path(app.config["BW"].folders.uploads)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "big.png").write_bytes(b"x" * (downloads.LARGE_BODY + 1))
    (folder / "small.png").write_bytes(b"x" * 1000)
    places = app.extensions["bananawiki.downloads"] = downloads.Places(2, 1)

    def get(addr: str, name: str = "big.png"):
        client = app.test_client()
        client.environ_base["REMOTE_ADDR"] = addr
        return client.get(f"/static/uploads/{name}")

    first = get("203.0.113.1")
    assert first.status_code == 200 and places.active == 1
    same = get("203.0.113.1")
    assert same.status_code == 429 and same.headers["Retry-After"] == "5", "one client holds at most its share"
    other = get("198.51.100.1")
    assert other.status_code == 200 and places.active == 2
    busy = get("192.0.2.1")
    assert busy.status_code == 503 and busy.headers["Retry-After"] == "5"
    assert get("192.0.2.1", "small.png").status_code == 200, "a small body fits in the socket buffers"
    assert app.test_client().get("/health").status_code == 200, "short requests keep their threads"
    first.close()  # the server is done with it: the file wrapper's close() gives the place back
    assert places.active == 1 and get("192.0.2.1").status_code == 200


def test_iw2_4_the_servers_run_threads_for_the_places(monkeypatch):
    import importlib
    import runpy
    from pathlib import Path

    import bananawiki.ops.gunicorn_conf as conf
    from bananawiki.core import downloads
    from bananawiki.wiki.config import load_config

    monkeypatch.setenv("BW_THREADS", "3")
    monkeypatch.setenv("BW_DOWNLOAD_THREADS", "5")
    assert importlib.reload(conf).threads == 8
    assert load_config({"BW_DOWNLOAD_THREADS": "5", "SECRET_KEY": "k" * 40}).download_threads == 5
    monkeypatch.delenv("BW_THREADS")
    monkeypatch.delenv("BW_DOWNLOAD_THREADS")
    assert importlib.reload(conf).threads == 12
    from bananawiki.hosting import wikihosts

    settings = runpy.run_path(str(Path(__file__).resolve().parents[2] / "hosting" / "gunicorn.conf.py"))
    assert settings["threads"] == 4 + downloads.PORTAL_PLACES + wikihosts.WAITING_PLACES  # NW4-2: proxy waits


def test_iw2_4_the_portal_holds_its_downloads_to_its_places(portal):  # noqa: F811
    from flask import Response

    from bananawiki.core import downloads
    from bananawiki.hosting import auth

    portal.add_url_rule("/iw2-big", "iw2_big", auth.public(lambda: Response(b"x" * (downloads.LARGE_BODY + 1))))
    places = portal.extensions["bananawiki.hosting.downloads"] = downloads.Places(1, 1)
    first = portal.test_client().get("/iw2-big")
    assert first.status_code == 200 and places.active == 1
    other = portal.test_client()
    other.environ_base["REMOTE_ADDR"] = "198.51.100.1"
    assert other.get("/iw2-big").status_code == 503
    first.close()
    assert places.active == 0 and other.get("/iw2-big").status_code == 200
