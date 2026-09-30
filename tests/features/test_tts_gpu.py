"""Remote GPU backend (fake transport), the standalone GPU server and the worker command."""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

from bananawiki.core import http as core_http
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import service as pages
from bananawiki.wiki.features.tts import backends, options, service, worker

ROOT = Path(__file__).resolve().parents[2]
MP3 = b"ID3" + b"\x01" * 300


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


class FakeFallback:
    name = "piper"

    def __init__(self, problem=None):
        self._problem = problem
        self.used = False

    def problem(self):
        return self._problem

    def synthesize(self, spoken, language, workdir):
        self.used = True
        path = workdir / ".part-fallback.wav"
        path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        return backends.Audio(path, "wav")


def gpu(transport, fallback=None, url="http://100.64.0.2:8787", token="token-123"):
    return backends.RemoteGpuBackend(url, token, 60, transport=transport, fallback=fallback)


def test_gpu_success_sends_token_and_pins_private_policy(tmp_path):
    transport = FakeTransport(core_http.HttpResponse(200, {"content-type": "audio/mpeg"}, MP3))
    audio = gpu(transport).synthesize("Hello there", "en", tmp_path)
    assert audio.extension == "mp3" and audio.path.read_bytes() == MP3
    method, url, kwargs = transport.calls[0]
    assert method == "POST" and url == "http://100.64.0.2:8787/synthesize"
    assert kwargs["headers"]["Authorization"] == "Bearer token-123"
    assert kwargs["allow_private"] is True and kwargs["schemes"] == ("http", "https")
    assert json.loads(kwargs["body"]) == {"text": "Hello there", "language": "en"}


@pytest.mark.parametrize(("status", "retryable", "rate_limited"), [(429, True, True), (401, False, False),
                                                                   (400, False, False), (503, True, False)])
def test_gpu_error_classification(tmp_path, status, retryable, rate_limited):
    transport = FakeTransport(core_http.HttpResponse(status, {}, b'{"error": "x"}'))
    with pytest.raises(backends.SynthesisError) as caught:
        gpu(transport).synthesize("Hello", "en", tmp_path)
    assert caught.value.retryable is retryable and caught.value.rate_limited is rate_limited


def test_gpu_rejects_non_audio_and_network_errors(tmp_path):
    with pytest.raises(backends.SynthesisError, match="MP3 or WAV"):
        gpu(FakeTransport(core_http.HttpResponse(200, {}, b"<html>" + b"x" * 200))).synthesize("Hi", "en", tmp_path)
    with pytest.raises(backends.SynthesisError) as caught:
        gpu(FakeTransport(error=core_http.BlockedDestination("metadata"))).synthesize("Hi", "en", tmp_path)
    assert caught.value.retryable is False
    with pytest.raises(backends.SynthesisError) as caught:
        gpu(FakeTransport(error=core_http.HttpError("timeout"))).synthesize("Hi", "en", tmp_path)
    assert caught.value.retryable is True
    assert list(tmp_path.iterdir()) == []


def test_gpu_falls_back_to_local_piper(tmp_path):
    fallback = FakeFallback()
    audio = gpu(FakeTransport(error=core_http.HttpError("down")), fallback).synthesize("Hi", "en", tmp_path)
    assert fallback.used and audio.extension == "wav"
    with pytest.raises(backends.SynthesisError):
        gpu(FakeTransport(error=core_http.HttpError("down")), FakeFallback("not installed")).synthesize(
            "Hi", "en", tmp_path)


def test_gpu_configuration_problems():
    assert gpu(FakeTransport(), url="http://host/x?y=1").problem()
    assert gpu(FakeTransport(), url="http://user:pw@host").problem()
    assert gpu(FakeTransport(), url="ftp://host").problem()
    assert gpu(FakeTransport(), token="").problem()
    assert gpu(FakeTransport(), token="bad token").problem()
    assert gpu(FakeTransport()).problem() is None


def test_http_policy_blocks_metadata_and_allows_private_services():
    import ipaddress

    allowed = core_http.address_allowed
    assert not allowed(ipaddress.ip_address("169.254.169.254"), allow_private=True)
    assert allowed(ipaddress.ip_address("100.64.0.2"), allow_private=True)
    assert allowed(ipaddress.ip_address("10.0.0.5"), allow_private=True)
    assert not allowed(ipaddress.ip_address("10.0.0.5"), allow_private=False)


def test_gpu_settings_ignore_tenant_columns_on_managed_hosting(app, db):
    db.execute("UPDATE site_settings SET tts_gpu_enabled = 1, tts_gpu_url = 'http://evil.example', "
               "tts_gpu_auth_token = 'tenant-token'")
    with app.test_request_context(), connection_scope():
        assert options.backend_name() == "remote-gpu"
        assert options.gpu_settings()[0] == "http://evil.example"
        cfg = options.config()
        app.extensions["bananawiki.tts.config"] = options.TtsConfig(**{**cfg.__dict__, "gpu_managed_by_host": True})
        assert options.gpu_settings()[:2] == ("", "")
        assert options.backend_name() == "piper"


# ── Standalone GPU server ─────────────────────────────────────────────────────


def load_server():
    path = ROOT / "contrib" / "tts-gpu-server" / "server.py"
    spec = importlib.util.spec_from_file_location("bananawiki_tts_gpu_server", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeEngine:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, language):
        self.calls.append((text, language))
        return MP3, "audio/mpeg"

    def details(self):
        return {"gpu_available": True, "loaded_voices": ["en"]}


@pytest.fixture
def gpu_server():
    module = load_server()
    settings = module.Settings.from_env({"TTS_AUTH_TOKEN": "x" * 32, "TTS_PORT": "1", "TTS_MAX_INPUT_CHARS": "50",
                                         "TTS_MAX_BODY_BYTES": "2048"})
    settings = module.Settings(**{**settings.__dict__, "port": 0})
    engine = FakeEngine()
    server = module.make_server(settings, engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], engine, module
    server.shutdown()
    server.server_close()


def call(port, method, path, body=None, token="x" * 32, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    sent = {"Content-Type": "application/json"}
    if token:
        sent["Authorization"] = f"Bearer {token}"
    sent.update(headers or {})
    conn.request(method, path, body=body, headers=sent)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def test_gpu_server_requires_token(gpu_server):
    port, engine, _module = gpu_server
    body = json.dumps({"text": "Hello", "language": "en"})
    assert call(port, "POST", "/synthesize", body, token=None)[0] == 401
    assert call(port, "POST", "/synthesize", body, token="y" * 32)[0] == 403
    status, data = call(port, "POST", "/synthesize", body)
    assert status == 200 and data == MP3 and engine.calls == [("Hello", "en")]


def test_gpu_server_limits_and_validation(gpu_server):
    port, engine, _module = gpu_server
    assert call(port, "POST", "/synthesize", json.dumps({"text": "a" * 51, "language": "en"}))[0] == 413
    assert call(port, "POST", "/synthesize", json.dumps({"text": "a" * 3000}))[0] == 413
    assert call(port, "POST", "/synthesize", json.dumps({"text": "hi", "language": "../x"}))[0] == 400
    assert call(port, "POST", "/synthesize", "not json")[0] == 400
    assert call(port, "POST", "/synthesize", json.dumps({"text": "hi"}),
                headers={"Content-Type": "text/plain"})[0] == 415
    assert engine.calls == []


def test_gpu_server_health_hides_details_without_token(gpu_server):
    port, _engine, _module = gpu_server
    status, data = call(port, "GET", "/health", token=None)
    assert status == 200 and json.loads(data) == {"status": "ok", "version": "1.6.0"}
    assert json.loads(call(port, "GET", "/health")[1])["gpu_available"] is True


def test_gpu_server_refuses_to_start_without_token():
    module = load_server()
    with pytest.raises(SystemExit):
        module.Settings.from_env({})
    with pytest.raises(SystemExit):
        module.Settings.from_env({"TTS_AUTH_TOKEN": "short"})


# ── Worker command ────────────────────────────────────────────────────────────


def test_worker_main_processes_queue_once(app, tmp_path, monkeypatch, db):
    with app.test_request_context(), connection_scope():
        page = pages.create("Worker page", "Words for the worker to read aloud today.", author_id=None)
        service.request(page, requested_by=None)
    for key, value in {"BW_ENV": "test", "BW_INSTANCE_DIR": str(tmp_path / "instance"),
                       "BW_DATABASE_PATH": app.config["BW"].database_path,
                       "SECRET_KEY": "test-secret-key-" + "x" * 32, "BW_TTS_BACKEND": "stub",
                       "BW_BACKGROUND_JOBS": "0"}.items():
        monkeypatch.setenv(key, value)
    assert worker.main(["--once"]) == 0
    row = db.one("SELECT * FROM tts_generations WHERE page_id = ?", (page["id"],))
    assert row["status"] == "completed" and row["file_size"] > 0


def test_worker_script_exists():
    source = (ROOT / "scripts" / "tts_worker.py").read_text()
    assert "bananawiki.wiki.features.tts.worker import main" in source


def test_gpu_server_accepts_per_wiki_tokens_and_revokes_single_wikis():
    from bananawiki.hosting.instances import tenant_tts_token

    module = load_server()
    master = "m" * 32
    settings = module.Settings.from_env({"TTS_AUTH_TOKEN": master, "TTS_REVOKED_TENANTS": "badwiki1"})
    assert module.token_valid(settings, master)
    token = tenant_tts_token(master, "goodwiki1")
    assert token == module.tenant_token(master, "goodwiki1"), "portal and server derive the same token"
    assert module.token_valid(settings, token)
    assert not module.token_valid(settings, tenant_tts_token(master, "badwiki1"))
    assert not module.token_valid(settings, tenant_tts_token("other" * 8, "goodwiki1"))
    assert not module.token_valid(settings, token.replace("goodwiki1", "goodwiki2"))
