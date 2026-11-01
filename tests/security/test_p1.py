"""Regression tests for the PW1 findings of the full re-audit pass 10 (request bodies, access log, onboarding)."""

from __future__ import annotations

import importlib
import json
import runpy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from werkzeug.wrappers import Request

REPO = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024

# PW1-1: GET and HEAD bodies keep the 500 MiB ceiling


def _multipart(size: int) -> tuple[bytes, dict[str, str]]:
    body = (b'--b\r\nContent-Disposition: form-data; name="f"; filename="x"\r\n\r\n' + b"A" * size
            + b"\r\n--b--\r\n")
    return body, {"Content-Type": "multipart/form-data; boundary=b"}


@pytest.fixture
def small_body_wiki(app_factory, make_user, login):
    app = app_factory(environ={"BW_MAX_CONTENT_LENGTH_BYTES": str(MIB)})

    @app.route("/_probe/get-form", methods=["GET", "HEAD"])
    def get_form():
        from flask import request

        return str(sum(len(part.read()) for part in request.files.values()))

    client = app.test_client()
    login(client, make_user("bob"))
    return app, client


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_pw1_1_safe_method_bodies_get_the_ordinary_cap(small_body_wiki, method):
    app, client = small_body_wiki
    assert app.config["MAX_CONTENT_LENGTH"] > 2 * MIB
    body, headers = _multipart(2 * MIB)
    assert client.open("/_probe/get-form", method=method, data=body, headers=headers).status_code == 413
    small, headers = _multipart(1000)
    assert client.open("/_probe/get-form", method=method, data=small, headers=headers).status_code == 200


def test_pw1_1_the_merge_request_page_does_not_parse_a_get_body(small_body_wiki, monkeypatch):
    _, client = small_body_wiki
    parsed = []
    original = Request._load_form_data
    monkeypatch.setattr(Request, "_load_form_data", lambda self: (parsed.append(self.method), original(self)))
    body, headers = _multipart(2 * MIB)
    response = client.get("/settings/merge-request/from/carol", data=body, headers=headers)
    assert response.status_code == 200
    assert 'value="carol"' in response.get_data(as_text=True)
    assert "GET" not in parsed
    assert client.post("/settings/merge-request", data=body, headers=headers).status_code == 413


# PW1-2: Gunicorn's access log writes C1 control characters raw

CONFIGS = {
    "wiki": lambda: vars(importlib.reload(importlib.import_module("bananawiki.ops.gunicorn_conf"))),
    "root": lambda: runpy.run_path(str(REPO / "gunicorn.conf.py")),
    "portal": lambda: runpy.run_path(str(REPO / "hosting" / "gunicorn.conf.py")),
}


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_pw1_2_the_access_log_escapes_control_characters(name, tmp_path, monkeypatch):
    util = pytest.importorskip("gunicorn.util")
    from gunicorn.config import Config

    monkeypatch.setattr(util, "close_graceful", util.close_graceful)  # the configurations replace it
    settings = CONFIGS[name]()
    config = Config()
    config.set("logger_class", settings.get("logger_class", "gunicorn.glogging.Logger"))
    log_file = tmp_path / "access.log"
    config.set("accesslog", str(log_file))
    logger = config.logger_class(config)
    environ = {"REQUEST_METHOD": "GET", "RAW_URI": "/health?\x9b31mQ\x9d0;t\x9c", "SERVER_PROTOCOL": "HTTP/1.1",
               "REMOTE_ADDR": "127.0.0.1", "HTTP_REFERER": "http://r/\x9d52;c;aGk=\x9c",
               "HTTP_USER_AGENT": "UA\x9b2J\x1b]0;x\x07\r\nfake line"}
    response = SimpleNamespace(status="200 OK", sent=2, headers=[])
    request = SimpleNamespace(headers=[("USER-AGENT", environ["HTTP_USER_AGENT"])])
    logger.access(response, request, environ, timedelta(milliseconds=5))
    for handler in logger.access_log.handlers:
        handler.flush()
    line = log_file.read_text(encoding="utf-8")
    assert line.count("\n") == 1
    assert not any(0x80 <= ord(char) <= 0x9F or (ord(char) < 0x20 and char != "\n") for char in line)
    assert "/health?\\x9b31mQ\\x9d0;t\\x9c" in line and "UA\\x9b2J\\x1b]0;x\\x07" in line
    for handler in logger.access_log.handlers:
        handler.close()


# PW1-3: replaying the onboarding wizard changes site settings with no audit entry


def test_pw1_3_the_onboarding_wizard_records_the_settings_it_changes(app, make_user, login, db):
    client = app.test_client()
    login(client, make_user("plainadmin", role="admin"))
    since = int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
    response = client.post("/onboarding", data={"site_name": "Hijacked Name", "language": "en",
                                                 "default_theme_mode": "light", "mode": "easy",
                                                 "new_user_intro_enabled": "1"})
    assert response.status_code == 302
    assert db.scalar("SELECT site_name FROM site_settings WHERE id = 1") == "Hijacked Name"
    rows = db.all("SELECT details FROM audit_log WHERE id > ? AND action = 'settings.updated'", (since,))
    assert len(rows) == 1
    details = json.loads(rows[0]["details"])
    assert details["via"] == "onboarding"
    assert set(details["changed"].split(", ")) >= {"site_name", "default_theme_mode", "new_user_intro_enabled"}
