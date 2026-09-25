"""Security regressions for the TTS plugin and the remote GPU backend.

* A hosted wiki takes the remote GPU URL and token only from the host's
  environment, never from its own ``tts_gpu_*`` settings, so a tenant admin
  cannot send the platform's shared token (or the worker's requests) to a
  server of their choosing.  Requests stay on the configured host.
* The host's TTS switches (``BW_MANAGED_TTS_DISABLED`` and EasyWiki) hold on
  every path that queues or synthesizes audio, not only the Generate button.
* The companion GPU server compares tokens in constant time, refuses to start
  without one and listens on loopback by default.
"""

from __future__ import annotations

import http.server
import importlib.util
import ipaddress
import os
import sys
import threading
import types

import pytest

import config
import db

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GPU_DIR = os.path.join(REPO_ROOT, "banana-tts-gpu")

PLATFORM_TOKEN = "PLATFORM-SHARED-GPU-TOKEN"
TENANT_TOKEN = "TENANT-CHOSEN-TOKEN"
# One MPEG audio frame header followed by padding: enough for the backend's
# "looks like audio" and minimum-size checks.
FAKE_MP3 = b"\xff\xfb\x90\x44" + b"\x00" * 400


class _Sink(http.server.BaseHTTPRequestHandler):
    """Records every request; replies with whatever the test put in ``body``."""

    seen = []
    body = FAKE_MP3

    def do_POST(self):  # noqa: N802 (http.server naming)
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
        })
        payload = type(self).body
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def sink():
    """Start a local HTTP listener standing in for a GPU or internal service."""
    handler = type("Sink", (_Sink,), {"seen": [], "body": FAKE_MP3})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    handler.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield handler
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def clean_tts_env(monkeypatch, tmp_path):
    """No TTS overrides from the developer's shell; a private audio folder."""
    for name in (
        "BW_TTS_BACKEND",
        "BW_TTS_REMOTE_GPU_URL",
        "BW_TTS_REMOTE_GPU_AUTH_TOKEN",
        "BW_TTS_REMOTE_GPU_TIMEOUT",
        "BW_TTS_INLINE_WORKER",
        "BW_INSTANCE_DIR",
        "BW_EASY_WIKI",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "MANAGED_HOSTING", False)
    monkeypatch.setattr(config, "MANAGED_TTS_DISABLED", False)
    folder = tmp_path / "tts"
    folder.mkdir()
    monkeypatch.setattr(config, "TTS_FOLDER", str(folder))
    import helpers._tts as tts
    tts._reset_tts_runtime_state_for_tests()
    yield str(folder)
    tts._reset_tts_runtime_state_for_tests()


@pytest.fixture
def local_piper_stub(monkeypatch):
    """Replace local Piper with a stub so a fallback never loads real voices."""
    import helpers._tts as tts
    calls = []

    def _fake_piper(text, language, dest_path):
        calls.append(dest_path)
        tts._synthesize_with_stub(text, language, dest_path)

    monkeypatch.setattr(tts, "_synthesize_with_piper", _fake_piper)
    return calls


def _plant_tenant_gpu_settings(url, token=TENANT_TOKEN):
    """Write tts_gpu_* the way a tenant admin can (a full-site backup import)."""
    db.update_site_settings(
        tts_gpu_enabled=1, tts_gpu_url=url,
        tts_gpu_auth_token=token, tts_gpu_timeout=5,
    )


# Remote GPU configuration on hosted wikis (B#0, A#35)

@pytest.mark.parametrize("hosted_by", ["instance_dir", "managed_hosting"])
def test_hosted_wiki_never_uses_tenant_gpu_settings(
    monkeypatch, tmp_path, clean_tts_env, sink, hosted_by,
):
    import helpers._tts as tts
    if hosted_by == "instance_dir":
        monkeypatch.setenv("BW_INSTANCE_DIR", str(tmp_path))
    else:
        monkeypatch.setattr(config, "MANAGED_HOSTING", True)
    # The platform's own token must not be usable either when some older
    # hosting code left it in the tenant database.
    _plant_tenant_gpu_settings(sink.url, token=PLATFORM_TOKEN)

    assert tts.remote_gpu_settings_managed_by_host()
    assert tts._remote_gpu_config() == ("", "", 120)
    assert tts._selected_backend() == "piper"
    with pytest.raises(RuntimeError):
        tts._synthesize_with_remote_gpu("hello", "en", str(tmp_path / "out.mp3"))
    assert sink.seen == []


def test_hosted_settings_page_hides_stored_gpu_values(
    monkeypatch, tmp_path, clean_tts_env, logged_in_admin,
):
    monkeypatch.setenv("BW_INSTANCE_DIR", str(tmp_path))
    _plant_tenant_gpu_settings("http://10.9.8.7:8787")

    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    assert TENANT_TOKEN[:8].encode() not in resp.data
    assert b"10.9.8.7" not in resp.data
    assert b'name="tts_gpu_url"' not in resp.data
    assert b"BW_TTS_REMOTE_GPU_URL" in resp.data


def test_self_hosted_settings_page_shows_gpu_fields(clean_tts_env, logged_in_admin):
    _plant_tenant_gpu_settings("http://10.9.8.7:8787")

    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    assert b'name="tts_gpu_url"' in resp.data
    assert b"10.9.8.7" in resp.data


def test_hosted_wiki_uses_host_env_and_ignores_tenant_url(
    monkeypatch, tmp_path, clean_tts_env, sink,
):
    import helpers._tts as tts
    monkeypatch.setenv("BW_INSTANCE_DIR", str(tmp_path))
    monkeypatch.setenv("BW_TTS_REMOTE_GPU_URL", sink.url)
    monkeypatch.setenv("BW_TTS_REMOTE_GPU_AUTH_TOKEN", PLATFORM_TOKEN)
    # The tenant tries to redirect the platform's requests elsewhere.
    _plant_tenant_gpu_settings("http://127.0.0.1:9/attacker", token=TENANT_TOKEN)

    assert tts._selected_backend() == "remote-gpu"
    dest = tmp_path / "out.mp3"
    tts._synthesize_with_remote_gpu("hello", "en", str(dest))

    assert sink.seen == [{"path": "/synthesize", "auth": f"Bearer {PLATFORM_TOKEN}"}]
    assert dest.read_bytes() == FAKE_MP3


def test_hosted_full_chain_does_not_reach_tenant_url(
    monkeypatch, tmp_path, clean_tts_env, sink, local_piper_stub,
    admin_user, logged_in_admin,
):
    """The boundary review's chain: tenant URL, Generate, worker, audio download."""
    import helpers._tts as tts
    monkeypatch.setenv("BW_INSTANCE_DIR", str(tmp_path))
    sink.body = b"SECRET-INTERNAL-JSON:" + b"x" * 200
    _plant_tenant_gpu_settings(f"{sink.url}/internal/admin?q=", token=PLATFORM_TOKEN)
    db.update_site_settings(tts_page_panel_enabled=1)
    page_id = db.create_page("Hello", "hello", "Some words to read aloud.", None, admin_user)

    resp = logged_in_admin.post("/page/hello/tts/generate", json={"language": "en"})
    assert resp.status_code == 202
    assert tts.process_next_tts_generation(clean_tts_env) is True

    row = db.get_tts_generation(page_id)
    assert row["status"] == "completed"
    audio = logged_in_admin.get("/page/hello/tts/audio")
    assert audio.status_code == 200
    assert not audio.data.startswith(b"SECRET-INTERNAL-JSON")
    assert sink.seen == []
    assert local_piper_stub, "local Piper should have produced the audio"


def test_self_hosted_wiki_keeps_admin_gpu_settings(
    tmp_path, clean_tts_env, sink,
):
    import helpers._tts as tts
    _plant_tenant_gpu_settings(sink.url, token="self-hosted-token")

    assert not tts.remote_gpu_settings_managed_by_host()
    assert tts._selected_backend() == "remote-gpu"
    dest = tmp_path / "out.mp3"
    tts._synthesize_with_remote_gpu("hello", "en", str(dest))
    assert sink.seen == [{"path": "/synthesize", "auth": "Bearer self-hosted-token"}]


def test_self_hosted_env_still_wins_over_settings(
    monkeypatch, tmp_path, clean_tts_env, sink,
):
    import helpers._tts as tts
    monkeypatch.setenv("BW_TTS_REMOTE_GPU_URL", sink.url + "/")
    monkeypatch.setenv("BW_TTS_REMOTE_GPU_AUTH_TOKEN", "env-token")
    monkeypatch.setenv("BW_TTS_REMOTE_GPU_TIMEOUT", "30")
    _plant_tenant_gpu_settings("http://127.0.0.1:9", token="db-token")

    assert tts._remote_gpu_config() == (sink.url, "env-token", 30)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1/internal/admin?q=",
    "http://127.0.0.1:1/?",
    "http://127.0.0.1:1/#frag",
    "http://user:pass@127.0.0.1:1",
    "ftp://127.0.0.1:1",
    "http://127.0.0.1:99999",
    "http:///synthesize",
])
def test_gpu_url_cannot_choose_path_or_carry_extras(url):
    import helpers._tts as tts
    with pytest.raises(RuntimeError):
        tts._remote_gpu_endpoint(url)


@pytest.mark.parametrize("url,endpoint", [
    ("http://100.64.0.10:8787", "http://100.64.0.10:8787/synthesize"),
    ("https://gpu.example:8443/tts/", "https://gpu.example:8443/tts/synthesize"),
    ("http://[fd7a:115c::1]:8787", "http://[fd7a:115c::1]:8787/synthesize"),
])
def test_gpu_endpoint_for_plain_base_urls(url, endpoint):
    import helpers._tts as tts
    assert tts._remote_gpu_endpoint(url) == endpoint


def test_self_hosted_query_url_is_refused_without_a_request(
    tmp_path, clean_tts_env, sink,
):
    import helpers._tts as tts
    _plant_tenant_gpu_settings(f"{sink.url}/portal/internal/endpoint?x=")
    with pytest.raises(RuntimeError):
        tts._synthesize_with_remote_gpu("hello", "en", str(tmp_path / "out.mp3"))
    assert sink.seen == []


@pytest.mark.parametrize("address,allowed", [
    ("169.254.169.254", False),
    ("fe80::1", False),
    ("224.0.0.1", False),
    ("0.0.0.0", False),
    ("240.0.0.1", False),
    ("127.0.0.1", True),
    ("10.0.0.1", True),
    ("192.168.1.20", True),
    ("100.64.0.10", True),
    ("fd7a:115c:a1e0::1", True),
])
def test_gpu_address_policy(address, allowed):
    import helpers._tts as tts
    assert tts._remote_gpu_address_allowed(ipaddress.ip_address(address)) is allowed


def test_gpu_request_to_metadata_address_is_refused(
    monkeypatch, tmp_path, clean_tts_env,
):
    import helpers._tts as tts
    import http_transport

    def _no_dial(*args, **kwargs):
        raise AssertionError("a refused address must never be dialled")

    monkeypatch.setattr(http_transport, "_dial", _no_dial)
    monkeypatch.setenv("BW_TTS_REMOTE_GPU_URL", "http://169.254.169.254")
    monkeypatch.setenv("BW_TTS_REMOTE_GPU_AUTH_TOKEN", "env-token")
    with pytest.raises(RuntimeError, match="Refusing"):
        tts._synthesize_with_remote_gpu("hello", "en", str(tmp_path / "out.mp3"))


def test_gpu_reply_that_is_not_audio_is_not_saved(
    tmp_path, clean_tts_env, sink,
):
    import helpers._tts as tts
    sink.body = b'{"internal": "' + b"x" * 300 + b'"}'
    _plant_tenant_gpu_settings(sink.url, token="self-hosted-token")
    dest = tmp_path / "out.mp3"
    with pytest.raises(RuntimeError, match="not MP3 or WAV"):
        tts._synthesize_with_remote_gpu("hello", "en", str(dest))
    assert not dest.exists()


@pytest.mark.parametrize("data,is_audio", [
    (b"ID3\x04\x00" + b"\x00" * 200, True),
    (b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 200, True),
    (b"\xff\xfb\x90\x44" + b"\x00" * 200, True),   # MPEG-1 Layer III
    (b"\xff\xf3\x64\xc4" + b"\x00" * 200, True),   # MPEG-2 Layer III
    # Text in UTF-16 starts with 0xFF 0xFE, which passes the bare 11-bit sync.
    ('{"secret": "value"}'.encode("utf-16") + b"\x00" * 200, False),
    (b"\xff\xfd\x90\x44" + b"\x00" * 200, False),  # Layer II, not what lame writes
    (b"\xff\xfb\xf0\x44" + b"\x00" * 200, False),  # invalid bitrate index
    (b"\xff\xfb\x9c\x44" + b"\x00" * 200, False),  # reserved sample rate
    (b"\xff\xd8\xff\xe0" + b"\x00" * 200, False),  # JPEG
    (b"<html>" + b"\x00" * 200, False),
])
def test_audio_sniff_accepts_only_mp3_or_wav(data, is_audio):
    import helpers._tts as tts
    assert tts._looks_like_audio(data) is is_audio


def test_gpu_token_with_control_characters_is_refused_without_leaking(
    monkeypatch, tmp_path, clean_tts_env, sink,
):
    import helpers._tts as tts
    monkeypatch.setenv("BW_TTS_REMOTE_GPU_URL", sink.url)
    monkeypatch.setenv("BW_TTS_REMOTE_GPU_AUTH_TOKEN", "SECRET-PART\r\nX-Injected: 1")
    with pytest.raises(RuntimeError) as info:
        tts._synthesize_with_remote_gpu("hello", "en", str(tmp_path / "out.mp3"))
    assert "SECRET-PART" not in str(info.value)
    assert sink.seen == []


@pytest.mark.parametrize("raw,expected", [
    ("abc", 120), ("0", 120), ("-5", 120), ("45", 45), ("999999", 3600),
])
def test_gpu_timeout_env_is_parsed_safely(monkeypatch, clean_tts_env, raw, expected):
    import helpers._tts as tts
    monkeypatch.setattr(config, "MANAGED_HOSTING", True)
    monkeypatch.setenv("BW_TTS_REMOTE_GPU_TIMEOUT", raw)
    assert tts._remote_gpu_config()[2] == expected


# Host TTS policy (B#2)

@pytest.fixture(params=["managed_tts_disabled", "easy_wiki"])
def host_policy(request, monkeypatch, clean_tts_env):
    if request.param == "easy_wiki":
        monkeypatch.setenv("BW_EASY_WIKI", "1")
    else:
        monkeypatch.setattr(config, "MANAGED_TTS_DISABLED", True)
    monkeypatch.setenv("BW_TTS_BACKEND", "stub")
    import helpers._tts as tts
    assert tts.tts_generation_disabled_by_host()
    return request.param


def _pending_row(page_id, text_title="Hello", text_body="Some words to read aloud."):
    """Plant a pending row as if it had been queued before the policy applied."""
    from helpers._tts import tts_content_hash, detect_tts_language, tts_normalize_text
    language = detect_tts_language(tts_normalize_text(text_title, text_body))
    row, created = db.request_tts_generation(
        page_id=page_id, language=language,
        content_hash=tts_content_hash(text_title, text_body, language),
        requested_by=None,
    )
    assert created
    return row


def test_policy_blocks_auto_generation_from_page_edits(
    host_policy, admin_user, logged_in_admin,
):
    """The boundary review's bypass: auto-generate on, then a normal web edit."""
    db.update_site_settings(tts_auto_generate_enabled=1, tts_page_panel_enabled=1)
    page_id = db.create_page("Hello", "hello", "Some words to read aloud.", None, admin_user)

    direct = logged_in_admin.post("/page/hello/tts/generate", json={"language": "en"})
    assert direct.status_code == (404 if host_policy == "easy_wiki" else 503)

    edit = logged_in_admin.post(
        "/page/hello/edit",
        data={"title": "Hello", "content": "Some other words to read aloud now."},
    )
    assert edit.status_code == 302
    assert db.get_tts_generation(page_id) is None


def test_policy_blocks_hooks_and_enqueue(host_policy, admin_user):
    from bananawiki_sdk import emit_hook
    import helpers._tts as tts
    db.update_site_settings(tts_auto_generate_enabled=1)
    page_id = db.create_page("Hello", "hello", "Some words to read aloud.", None, admin_user)
    page = db.get_page(page_id)

    emit_hook("after_page_create", page=page, user={"id": admin_user, "role": "admin"})
    assert db.get_tts_generation(page_id) is None
    assert tts.enqueue_auto_generation(page, config.TTS_FOLDER) is None
    assert db.get_tts_generation(page_id) is None

    from plugins.builtin.tts import _backfill_all_pages
    assert _backfill_all_pages() == 0
    assert db.get_tts_generation(page_id) is None


def test_policy_edit_still_drops_stale_audio(host_policy, admin_user):
    from bananawiki_sdk import emit_hook
    page_id = db.create_page("Hello", "hello", "Some words to read aloud.", None, admin_user)
    row = _pending_row(page_id)
    assert db.mark_tts_processing(row["id"])
    assert db.mark_tts_completed(row["id"], "old.mp3", 10)

    db.update_page(page_id, title="Hello", content="Changed words.", user_id=admin_user)
    emit_hook("after_page_update", page=db.get_page(page_id),
              user={"id": admin_user, "role": "admin"})
    assert db.get_tts_generation(page_id) is None


def test_policy_worker_never_claims_rows(host_policy, monkeypatch, admin_user):
    import helpers._tts as tts
    page_id = db.create_page("Hello", "hello", "Some words to read aloud.", None, admin_user)
    row = _pending_row(page_id)

    assert tts.process_next_tts_generation(config.TTS_FOLDER) is False
    assert tts.run_tts_worker_loop(config.TTS_FOLDER, once=True) == 0
    assert tts.recover_stuck_generations(config.TTS_FOLDER) == 0
    assert tts.recover_orphaned_pending_generations(config.TTS_FOLDER) == 0

    monkeypatch.setenv("BW_TTS_INLINE_WORKER", "1")
    handle = tts.run_generation_async(
        generation_id=row["id"], page_id=page_id, language=row["language"],
        content_hash=row["content_hash"], text="Some words to read aloud.",
        tts_folder=config.TTS_FOLDER,
    )
    # Nothing was queued: the handle comes back already finished.
    assert not handle.is_alive()
    tts._run_generation(
        row["id"], page_id, row["language"], row["content_hash"],
        "Some words to read aloud.", config.TTS_FOLDER,
    )

    after = db.get_tts_generation(page_id)
    assert after["status"] == "pending"
    assert not after["filename"]


def test_policy_worker_script_stays_idle(host_policy, admin_user, caplog):
    page_id = db.create_page("Hello", "hello", "Some words to read aloud.", None, admin_user)
    _pending_row(page_id)
    spec = importlib.util.spec_from_file_location(
        "bw_tts_worker_script", os.path.join(REPO_ROOT, "scripts", "tts_worker.py"),
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    with caplog.at_level("INFO", logger="bananawiki.tts"):
        assert worker.main(["--once", "--tts-folder", config.TTS_FOLDER]) == 0
    assert "processes no rows" in caplog.text
    assert db.get_tts_generation(page_id)["status"] == "pending"


def test_managed_policy_status_and_backfill(
    monkeypatch, clean_tts_env, admin_user, logged_in_admin,
):
    monkeypatch.setattr(config, "MANAGED_TTS_DISABLED", True)
    db.update_site_settings(tts_page_panel_enabled=1)
    page_id = db.create_page("Hello", "hello", "Some words to read aloud.", None, admin_user)

    status = logged_in_admin.get("/page/hello/tts/status").get_json()
    assert status["can_generate"] is False
    assert status["generate_blocked_reason"] == "tts_disabled_by_hosting"
    assert status["generate_blocked_message"]

    resp = logged_in_admin.post("/admin/tts-status/backfill")
    assert resp.status_code == 302
    assert db.get_tts_generation(page_id) is None


def test_without_policy_auto_generation_still_queues(
    monkeypatch, clean_tts_env, admin_user,
):
    """Control: the checks above must not switch TTS off for everyone."""
    from bananawiki_sdk import emit_hook
    import helpers._tts as tts
    monkeypatch.setenv("BW_TTS_BACKEND", "stub")
    assert not tts.tts_generation_disabled_by_host()
    db.update_site_settings(tts_auto_generate_enabled=1)
    page_id = db.create_page("Hello", "hello", "Some words to read aloud.", None, admin_user)
    emit_hook("after_page_create", page=db.get_page(page_id),
              user={"id": admin_user, "role": "admin"})
    assert db.get_tts_generation(page_id)["status"] == "pending"
    assert tts.process_next_tts_generation(config.TTS_FOLDER) is True
    assert db.get_tts_generation(page_id)["status"] == "completed"


# Companion GPU server (A#45)

def _load_gpu_config(monkeypatch, token):
    if token is None:
        monkeypatch.delenv("TTS_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("TTS_AUTH_TOKEN", token)
    monkeypatch.delenv("TTS_HOST", raising=False)
    spec = importlib.util.spec_from_file_location(
        "bw_tts_gpu_config", os.path.join(GPU_DIR, "config.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("token", [None, "", "   "])
def test_gpu_server_refuses_to_start_without_token(monkeypatch, token):
    with pytest.raises(SystemExit):
        _load_gpu_config(monkeypatch, token)


def test_gpu_server_listens_on_loopback_by_default(monkeypatch):
    assert _load_gpu_config(monkeypatch, "s3cret").HOST == "127.0.0.1"


class _HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _load_gpu_server(monkeypatch, token):
    """Import server.py against minimal FastAPI stand-ins (not installed here)."""

    class _App:
        def __init__(self, **kwargs):
            pass

        def _route(self, *args, **kwargs):
            return lambda fn: fn

        get = post = on_event = _route

    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = _App
    fastapi.Depends = lambda dependency: dependency
    fastapi.Header = lambda default=None, **kwargs: default
    fastapi.HTTPException = _HTTPException
    responses = types.ModuleType("fastapi.responses")
    responses.Response = object
    fastapi.responses = responses
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = object

    gpu_config = _load_gpu_config(monkeypatch, token)
    spec = importlib.util.spec_from_file_location(
        "bw_tts_gpu_server", os.path.join(GPU_DIR, "server.py"),
    )
    module = importlib.util.module_from_spec(spec)
    # server.py does ``import config``; give it the GPU server's own module,
    # and only while it imports, so the wiki's config is back in place for
    # anything else running in this process.
    with monkeypatch.context() as patched:
        patched.setitem(sys.modules, "fastapi", fastapi)
        patched.setitem(sys.modules, "fastapi.responses", responses)
        patched.setitem(sys.modules, "pydantic", pydantic)
        patched.setitem(sys.modules, "config", gpu_config)
        spec.loader.exec_module(module)
    return module


def test_gpu_server_token_check_is_constant_time(monkeypatch):
    server = _load_gpu_server(monkeypatch, "correct-horse-battery")
    compared = []
    real_compare = server.hmac.compare_digest

    def _spy(a, b):
        compared.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(server.hmac, "compare_digest", _spy)

    assert server.verify_token("Bearer correct-horse-battery") is None
    assert compared == [(b"correct-horse-battery", b"correct-horse-battery")]

    for header, status in [
        ("Bearer correct-horse-batterx", 403),
        ("Bearer töken-with-non-ascii", 403),
        ("Bearer", 401),
        ("Basic correct-horse-battery", 401),
        (None, 401),
    ]:
        with pytest.raises(_HTTPException) as info:
            server.verify_token(header)
        assert info.value.status_code == status


def test_gpu_server_asks_ffmpeg_for_an_mp3_container(monkeypatch):
    """ffmpeg cannot guess a container for ``pipe:1``; without -f it exits 234."""
    server = _load_gpu_server(monkeypatch, "correct-horse-battery")
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout=b"ID3mp3", stderr=b"")

    monkeypatch.setattr(server, "_find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(server.subprocess, "run", _fake_run)
    assert server._wav_to_mp3(b"RIFF....WAVE") == b"ID3mp3"
    argv = calls[0]
    assert argv[-3:] == ["-f", "mp3", "pipe:1"]
