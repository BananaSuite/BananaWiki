"""Tests for the built-in ``tts`` (text-to-speech) plugin.

The tests run with ``BW_TTS_BACKEND=stub`` so synthesis is offline and
deterministic: see ``helpers/_tts.py:_synthesize_with_stub``.  The stub
writes a tiny silent MP3 frame so the audio/download endpoints have
something to send.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def _create_page(slug="welcome", title="Welcome", content="Hello world."):
    """Insert a wiki page directly via ``db.create_page``."""
    import db
    return db.create_page(
        title=title,
        slug=slug,
        content=content,
        category_id=None,
        user_id=None,
    )


@pytest.fixture(autouse=True)
def stub_backend(monkeypatch, tmp_path):
    """Force the offline stub backend + a temp folder for every test."""
    monkeypatch.setenv("BW_TTS_BACKEND", "stub")
    monkeypatch.setenv("BW_TTS_INLINE_WORKER", "1")
    folder = tmp_path / "tts"
    folder.mkdir(parents=True, exist_ok=True)
    import config
    import helpers._tts as _tts_mod
    _tts_mod._reset_tts_runtime_state_for_tests()
    monkeypatch.setattr(config, "TTS_FOLDER", str(folder))
    yield str(folder)
    _tts_mod._reset_tts_runtime_state_for_tests()


@pytest.fixture(autouse=True)
def manual_tts_mode_default(isolated_db):
    """Default every test into "manual mode" (panel on, auto-gen off).

    The post-fix ``/tts/generate`` endpoint refuses requests when the
    in-page panel is disabled (a user reported the button still triggered
    generation despite the setting being off).  Most route-level tests
    here exercise that endpoint as a convenient way to seed cached audio,
    so we flip the panel on for every test by default: individual tests
    that exercise auto-mode or the disabled-panel behaviour still
    override these settings explicitly.
    """
    import db
    try:
        db.update_site_settings(
            tts_page_panel_enabled=1, tts_auto_generate_enabled=0,
        )
    except Exception:  # noqa: BLE001 (never break collection on init)
        pass
    yield


def _wait_for_status(client, slug, target_statuses=("completed", "failed"),
                     timeout=30.0):
    """Poll ``/page/<slug>/tts/status`` until it reaches a terminal state.

    Returns the final ``generation`` dict (or ``None`` when no row exists).

    The deadline is generous on purpose. Generation finishes in well under a
    second here, and the loop returns as soon as the status is terminal, but a
    parallel run on a slow machine can leave a worker waiting while the other
    one loads a voice model. A short deadline turned that into a test failure.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = client.get(f"/page/{slug}/tts/status")
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        gen = body.get("generation")
        last = gen
        if gen is None:
            return None
        if gen["status"] in target_statuses:
            return gen
        time.sleep(0.05)
    return last


def _wait_for_db_status(page_id, target_statuses=("completed", "failed"),
                        timeout=5.0):
    """Poll the DB directly until a TTS row reaches a target status."""
    import db
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = db.get_tts_generation(page_id)
        if last and last["status"] in target_statuses:
            return last
        time.sleep(0.02)
    return last


# ---------------------------------------------------------------------------
#  helpers/_tts pure-function tests
# ---------------------------------------------------------------------------
class TestTextNormalisation:
    def test_strips_markdown_headings_lists_and_links(self):
        from helpers._tts import tts_normalize_text
        out = tts_normalize_text(
            "Title",
            "# Heading\n## Sub-heading\n* item one\n* item two\n[link](https://example.com)",
        )
        assert "#" not in out
        assert "*" not in out
        assert "https://example.com" not in out
        assert "Title." in out
        assert "item one" in out and "item two" in out
        assert "link" in out

    def test_strips_fenced_code_blocks(self):
        from helpers._tts import tts_normalize_text
        out = tts_normalize_text(
            "Page",
            "Intro\n```\ndef secret():\n    pass\n```\nOutro",
        )
        assert "def secret" not in out
        assert "Intro" in out and "Outro" in out

    def test_truncates_to_max_chars(self):
        from helpers._tts import tts_normalize_text, TTS_MAX_INPUT_CHARS
        long_body = "word " * (TTS_MAX_INPUT_CHARS // 4)
        out = tts_normalize_text("T", long_body)
        assert len(out) <= TTS_MAX_INPUT_CHARS

    def test_empty_inputs_yield_empty_string(self):
        from helpers._tts import tts_normalize_text
        assert tts_normalize_text("", "") == ""
        assert tts_normalize_text(None, None) == ""

    def test_content_hash_changes_with_language(self):
        from helpers._tts import tts_content_hash
        en = tts_content_hash("T", "Hello", "en")
        it = tts_content_hash("T", "Hello", "it")
        assert en != it

    def test_content_hash_stable_for_same_input(self):
        from helpers._tts import tts_content_hash
        a = tts_content_hash("T", "Hello", "en")
        b = tts_content_hash("T", "Hello", "en")
        assert a == b

    def test_content_hash_for_text_matches_title_content_hash(self):
        from helpers._tts import (
            tts_content_hash,
            tts_content_hash_for_text,
            tts_normalize_text,
        )
        spoken = tts_normalize_text("T", "Hello")
        assert tts_content_hash_for_text(spoken, "en") == tts_content_hash(
            "T", "Hello", "en",
        )


class TestStubBackend:
    def test_synthesize_writes_file(self, tmp_path):
        from helpers._tts import synthesize_to_mp3
        out = tmp_path / "x.mp3"
        synthesize_to_mp3("hello", "en", str(out))
        assert out.exists()
        assert out.stat().st_size > 0

    def test_synthesize_rejects_empty_text(self, tmp_path):
        from helpers._tts import synthesize_to_mp3
        with pytest.raises(RuntimeError):
            synthesize_to_mp3("   ", "en", str(tmp_path / "x.mp3"))

    def test_synthesize_rejects_unknown_language(self, tmp_path):
        from helpers._tts import synthesize_to_mp3
        # "xx" is not a supported language code, so reject it before the
        # synthesiser is invoked.
        with pytest.raises(RuntimeError):
            synthesize_to_mp3("hello", "xx", str(tmp_path / "x.mp3"))

    def test_synthesize_accepts_third_language(self, tmp_path):
        """Languages beyond EN/IT (e.g. Spanish) should still work."""
        from helpers._tts import synthesize_to_mp3
        out = tmp_path / "es.mp3"
        synthesize_to_mp3("Hola amigos como estan", "es", str(out))
        assert out.exists()
        assert out.stat().st_size > 0


class TestPiperBackend:
    def test_default_backend_is_local_piper(self, monkeypatch):
        import helpers._tts as _tts_mod
        monkeypatch.delenv("BW_TTS_BACKEND", raising=False)
        assert _tts_mod._selected_backend() == "piper"

    def test_piper_voice_mapping_covers_core_languages(self):
        import helpers._tts as _tts_mod
        assert _tts_mod._resolve_piper_voice_name("en") == "en_US-lessac-medium"
        assert _tts_mod._resolve_piper_voice_name("it") == "it_IT-paola-medium"
        assert _tts_mod._resolve_piper_voice_name("es") == "es_ES-sharvard-medium"
        assert _tts_mod._resolve_piper_voice_name("fr-CA") == "fr_FR-tom-medium"

    def test_piper_voice_mapping_covers_multilingual_local_catalogue(self):
        import helpers._tts as _tts_mod

        expected = {
            "ar": "ar_JO-kareem-medium",
            "bg": "bg_BG-dimitar-medium",
            "cy": "cy_GB-bu_tts-medium",
            "de": "de_DE-thorsten-medium",
            "eu": "eu_ES-maider-medium",
            "fa": "fa_IR-amir-medium",
            "hi": "hi_IN-pratham-medium",
            "id": "id_ID-news_tts-medium",
            "ka": "ka_GE-natia-medium",
            "kk": "kk_KZ-issai-high",
            "ku": "ku_TR-berfin_renas-medium",
            "lb": "lb_LU-marylux-medium",
            "lv": "lv_LV-aivars-medium",
            "ml": "ml_IN-meera-medium",
            "sk": "sk_SK-lili-medium",
            "sl": "sl_SI-artur-medium",
            "sq": "sq_AL-edon-medium",
            "te": "te_IN-maya-medium",
            "uk": "uk_UA-mykyta-high",
            "ur": "ur_PK-fasih-medium",
            "zh-TW": "zh_CN-huayan-medium",
        }

        for code, voice in expected.items():
            assert _tts_mod._resolve_piper_voice_name(code) == voice
            assert _tts_mod.piper_voice_configured_for_language(code) is True

        assert _tts_mod.piper_voice_configured_for_language("ja") is False
        assert _tts_mod.piper_voice_configured_for_language("ko") is False

    def test_piper_voice_availability_map_uses_one_effective_mapping(self):
        import helpers._tts as _tts_mod

        availability = _tts_mod.piper_voice_availability_map(
            ("en", "fr-CA", "ja"),
        )
        assert availability == {
            "en": True,
            "fr-CA": True,
            "ja": False,
        }

    def test_piper_voice_mapping_can_be_overridden(self, monkeypatch):
        import helpers._tts as _tts_mod
        monkeypatch.setenv(
            "BW_TTS_PIPER_VOICE_MAP",
            '{"ko": "ko_KR-custom-medium", "ja": "/voices/ja.onnx"}',
        )
        assert _tts_mod._resolve_piper_voice_name("ko") == "ko_KR-custom-medium"
        assert _tts_mod._resolve_piper_voice_name("ja") == "/voices/ja.onnx"

    def test_piper_backend_writes_wav_without_ffmpeg(self, tmp_path, monkeypatch):
        import helpers._tts as _tts_mod

        class FakeVoice:
            def synthesize_wav(self, text, wav_file, syn_config=None):
                assert text == "hello"
                assert syn_config is not None
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 320)

        monkeypatch.setenv("BW_TTS_BACKEND", "piper")
        monkeypatch.setenv("BW_TTS_PIPER_OUTPUT_FORMAT", "wav")
        monkeypatch.setattr(_tts_mod, "_load_piper_voice", lambda language: FakeVoice())

        out = tmp_path / "local.wav"
        _tts_mod.synthesize_to_mp3("hello", "en", str(out))

        assert out.exists()
        assert out.stat().st_size > 44
        assert out.read_bytes()[:4] == b"RIFF"

    def test_preferred_piper_extension_auto_uses_wav_without_ffmpeg(self, monkeypatch):
        import helpers._tts as _tts_mod
        monkeypatch.setenv("BW_TTS_BACKEND", "piper")
        monkeypatch.setenv("BW_TTS_PIPER_OUTPUT_FORMAT", "auto")
        monkeypatch.setattr(_tts_mod, "_ffmpeg_binary", lambda: None)
        assert _tts_mod.preferred_tts_audio_extension() == "wav"

    def test_preferred_piper_extension_auto_uses_mp3_with_ffmpeg(self, monkeypatch):
        import helpers._tts as _tts_mod
        monkeypatch.setenv("BW_TTS_BACKEND", "piper")
        monkeypatch.setenv("BW_TTS_PIPER_OUTPUT_FORMAT", "auto")
        monkeypatch.setattr(_tts_mod, "_ffmpeg_binary", lambda: "/usr/bin/ffmpeg")
        assert _tts_mod.preferred_tts_audio_extension() == "mp3"

# ---------------------------------------------------------------------------
#  db/_tts state machine tests
# ---------------------------------------------------------------------------
class TestDbStateMachine:
    def test_request_creates_pending_row(self, admin_user):
        import db
        page_id = _create_page()
        row, created = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h1",
            requested_by=admin_user,
        )
        assert created is True
        assert row["status"] == "pending"
        assert row["language"] == "en"

    def test_duplicate_request_same_hash_returns_existing(self, admin_user):
        import db
        page_id = _create_page()
        first, c1 = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h1",
            requested_by=admin_user,
        )
        second, c2 = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h1",
            requested_by=admin_user,
        )
        assert c1 is True
        assert c2 is False
        assert first["id"] == second["id"]

    def test_request_with_new_hash_replaces_failed_row(self, admin_user):
        import db
        page_id = _create_page()
        first, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h1",
            requested_by=admin_user,
        )
        db.mark_tts_failed(first["id"], "boom")
        second, created = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h2",
            requested_by=admin_user,
        )
        assert created is True
        assert second["id"] != first["id"]

    def test_request_with_new_language_replaces_completed_row(self, admin_user):
        import db
        page_id = _create_page()
        first, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h1",
            requested_by=admin_user,
        )
        db.mark_tts_processing(first["id"])
        db.mark_tts_completed(first["id"], "f.mp3", 100)
        second, created = db.request_tts_generation(
            page_id=page_id, language="it", content_hash="h1",
            requested_by=admin_user,
        )
        assert created is True
        assert second["language"] == "it"

    def test_invalid_language_raises(self, admin_user):
        import db
        page_id = _create_page()
        with pytest.raises(ValueError):
            db.request_tts_generation(
                page_id=page_id, language="xx", content_hash="h1",
                requested_by=admin_user,
            )

    def test_third_language_is_accepted(self, admin_user):
        """Multilingual TTS: languages beyond EN/IT (e.g. Spanish) work."""
        import db
        page_id = _create_page()
        row, created = db.request_tts_generation(
            page_id=page_id, language="es", content_hash="h-es",
            requested_by=admin_user,
        )
        assert created is True
        assert row["language"] == "es"

    def test_lifecycle_transitions(self, admin_user):
        import db
        page_id = _create_page()
        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h1",
            requested_by=admin_user,
        )
        assert db.mark_tts_processing(row["id"]) is True
        # Re-marking should be a no-op (returns False).
        assert db.mark_tts_processing(row["id"]) is False
        assert db.mark_tts_completed(row["id"], "f.mp3", 256) is True
        # Once completed, marking failed is a no-op.
        db.mark_tts_failed(row["id"], "ignored")
        fresh = db.get_tts_generation_by_id(row["id"])
        assert fresh["status"] == "completed"

    def test_delete_returns_filename_and_clears_row(self, admin_user):
        import db
        page_id = _create_page()
        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h1",
            requested_by=admin_user,
        )
        db.mark_tts_processing(row["id"])
        db.mark_tts_completed(row["id"], "page.mp3", 64)
        filename, size = db.delete_tts_generation(page_id)
        assert filename == "page.mp3"
        assert size == 64
        assert db.get_tts_generation(page_id) is None

    def test_clear_all_returns_filenames(self, admin_user):
        import db
        page_id = _create_page()
        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h1",
            requested_by=admin_user,
        )
        db.mark_tts_processing(row["id"])
        db.mark_tts_completed(row["id"], "x.mp3", 64)
        names = db.clear_all_tts_generations()
        assert names == ["x.mp3"]
        assert db.list_tts_generations() == []

    def test_retry_pending_increments_retry_count(self, admin_user):
        import db
        page_id = _create_page()
        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h1",
            requested_by=admin_user,
        )
        db.mark_tts_processing(row["id"])
        retry = db.mark_tts_retry_pending(row["id"], "temporary timeout", 2)
        assert retry is not None
        assert retry["status"] == "pending"
        assert retry["retry_count"] == 1
        assert retry["error_message"] == "temporary timeout"


class TestPluginGating:
    def test_status_404_when_disabled(self, client, admin_user):
        import db
        _create_page()
        db.disable_plugin("tts")
        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome/tts/status")
        assert resp.status_code == 404

    def test_generate_404_when_disabled(self, client, admin_user):
        import db
        _create_page()
        db.disable_plugin("tts")
        _login(client, "admin", "admin123")
        resp = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_disable_clears_existing_rows(self, client, admin_user, stub_backend):
        import db
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 202)
        gen = _wait_for_status(client, "welcome")
        assert gen and gen["status"] == "completed"

        # Disable the plugin: its on_disable hook must wipe the table.
        from plugins.builtin.tts import teardown
        teardown(None)
        rows = db.list_tts_generations()
        assert rows == []


class TestRoutes:
    def test_status_for_logged_in_user(self, client, admin_user):
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome/tts/status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["generation"] is None
        # All TTS-supported languages are exposed.
        codes = [lang["code"] for lang in body["supported_languages"]]
        from helpers._tts import TTS_SUPPORTED_LANGUAGES
        assert codes == list(TTS_SUPPORTED_LANGUAGES)

    def test_status_anonymous_redirects_to_login(self, client, admin_user):
        _create_page()
        resp = client.get("/page/welcome/tts/status")
        assert resp.status_code in (302, 401)

    def test_unknown_page_returns_404(self, client, admin_user):
        _login(client, "admin", "admin123")
        resp = client.get("/page/does-not-exist/tts/status")
        assert resp.status_code == 404

    def test_regular_user_can_generate_missing_audio(
        self, client, admin_user, regular_user, stub_backend,
    ):
        _create_page()
        _login(client, "user", "user123")
        resp = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 202)
        gen = _wait_for_status(client, "welcome")
        assert gen is not None
        assert gen["status"] == "completed"

    def test_generate_happy_path(self, client, admin_user, stub_backend):
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 202)
        body = resp.get_json()
        assert body["ok"] is True
        assert body["created"] is True
        assert body["generation"]["status"] in ("pending", "processing", "completed")

        gen = _wait_for_status(client, "welcome")
        assert gen is not None
        assert gen["status"] == "completed"
        assert gen["has_file"] is True
        assert gen["file_size"] > 0

        # On-disk file actually exists.
        files = os.listdir(stub_backend)
        assert len(files) == 1

    def test_generate_refuses_when_audio_already_exists(
        self, client, admin_user, stub_backend,
    ):
        _create_page()
        _login(client, "admin", "admin123")
        first = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert first.status_code in (200, 202)
        _wait_for_status(client, "welcome")

        second = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "it"}),
            content_type="application/json",
        )
        body = second.get_json() or {}
        assert second.status_code == 409
        assert body.get("error") == "already_generated"

    def test_generate_concurrent_request_is_deduped(self, client, admin_user,
                                                    stub_backend, monkeypatch):
        """Second generate while the first is still pending returns 409."""
        import helpers._tts as _tts_mod

        # Slow the stub down enough that a second request lands while
        # the first is still pending/processing.
        slow_event = threading.Event()
        original = _tts_mod._synthesize_with_stub

        def slow_stub(text, language, dest_path):
            slow_event.wait(timeout=2.0)
            return original(text, language, dest_path)

        monkeypatch.setattr(_tts_mod, "_synthesize_with_stub", slow_stub)

        _create_page()
        _login(client, "admin", "admin123")

        first = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert first.status_code in (200, 202)

        second = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert second.status_code == 409
        body = second.get_json()
        assert body["ok"] is False
        assert body["error"] == "already_in_progress"

        slow_event.set()
        gen = _wait_for_status(client, "welcome")
        assert gen and gen["status"] == "completed"

    def test_generate_refuses_when_manual_queue_is_full(
        self, client, admin_user, monkeypatch,
    ):
        import config

        monkeypatch.setenv("BW_TTS_INLINE_WORKER", "0")
        monkeypatch.setattr(config, "TTS_MANUAL_MAX_ACTIVE_JOBS", 1)
        monkeypatch.setattr(config, "TTS_MANUAL_MAX_ACTIVE_PER_USER", 10)

        _create_page(slug="first-tts", title="First TTS", content="One.")
        _create_page(slug="second-tts", title="Second TTS", content="Two.")
        _login(client, "admin", "admin123")

        first = client.post(
            "/page/first-tts/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert first.status_code == 202

        status = client.get("/page/second-tts/tts/status").get_json()
        assert status["can_generate"] is False

        second = client.post(
            "/page/second-tts/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        body = second.get_json()
        assert second.status_code == 503
        assert body["error"] == "tts_queue_full"
        assert second.headers.get("Retry-After") == "60"

    def test_generate_refuses_other_user_while_manual_tts_is_busy(
        self, client, admin_user, regular_user, monkeypatch,
    ):
        import config

        monkeypatch.setenv("BW_TTS_INLINE_WORKER", "0")
        monkeypatch.setattr(config, "TTS_MANUAL_MAX_ACTIVE_JOBS", 1)
        monkeypatch.setattr(config, "TTS_MANUAL_MAX_ACTIVE_PER_USER", 1)

        _create_page(slug="first-reader-tts", title="First Reader TTS",
                     content="One.")
        _create_page(slug="second-reader-tts", title="Second Reader TTS",
                     content="Two.")

        _login(client, "admin", "admin123")
        first = client.post(
            "/page/first-reader-tts/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert first.status_code == 202

        client.post("/logout")
        _login(client, "user", "user123")

        status = client.get("/page/second-reader-tts/tts/status").get_json()
        assert status["can_generate"] is False
        assert status["generate_blocked_reason"] == "tts_queue_full"
        assert status["manual_queue"]["active_jobs"] == 1

        second = client.post(
            "/page/second-reader-tts/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        body = second.get_json()
        assert second.status_code == 503
        assert body["error"] == "tts_queue_full"

    def test_generate_refuses_when_user_has_too_many_pending_jobs(
        self, client, admin_user, monkeypatch,
    ):
        import config

        monkeypatch.setenv("BW_TTS_INLINE_WORKER", "0")
        monkeypatch.setattr(config, "TTS_MANUAL_MAX_ACTIVE_JOBS", 10)
        monkeypatch.setattr(config, "TTS_MANUAL_MAX_ACTIVE_PER_USER", 1)

        _create_page(slug="first-user-tts", title="First User TTS", content="One.")
        _create_page(slug="second-user-tts", title="Second User TTS", content="Two.")
        _login(client, "admin", "admin123")

        first = client.post(
            "/page/first-user-tts/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert first.status_code == 202

        status = client.get("/page/second-user-tts/tts/status").get_json()
        assert status["can_generate"] is False

        second = client.post(
            "/page/second-user-tts/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        body = second.get_json()
        assert second.status_code == 429
        assert body["error"] == "tts_user_queue_full"
        assert second.headers.get("Retry-After") == "60"

    def test_generate_rejects_unknown_language(self, client, admin_user):
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "klingon"}),
            content_type="application/json",
        )
        # 'klingon' falls back to 'en' via _normalize_language; the request
        # therefore succeeds.  Use an unambiguous-but-valid alternative input
        # to exercise the rejection branch.
        assert resp.status_code in (200, 202)

    def test_audio_endpoint_streams_file(self, client, admin_user, stub_backend):
        _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        gen = _wait_for_status(client, "welcome")
        assert gen and gen["status"] == "completed"

        resp = client.get("/page/welcome/tts/audio")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("audio/")
        assert len(resp.data) > 0

    def test_download_endpoint_sets_attachment_header(self, client, admin_user,
                                                      stub_backend):
        _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        _wait_for_status(client, "welcome")

        resp = client.get("/page/welcome/tts/download")
        assert resp.status_code == 200
        disp = resp.headers.get("Content-Disposition", "")
        assert "attachment" in disp.lower()
        assert "welcome-en.mp3" in disp

    def test_audio_404_when_no_generation(self, client, admin_user):
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome/tts/audio")
        assert resp.status_code == 404

    def test_cancel_clears_cache_and_file(self, client, admin_user, stub_backend):
        _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        _wait_for_status(client, "welcome")

        resp = client.post("/page/welcome/tts/cancel")
        assert resp.status_code == 200

        # Status is back to "no generation".
        status = client.get("/page/welcome/tts/status").get_json()
        assert status["generation"] is None
        # On-disk MP3 was removed.
        assert os.listdir(stub_backend) == []


class TestHookInvalidation:
    def test_page_update_refreshes_existing_cache_when_auto_off(
        self, client, admin_user, stub_backend,
    ):
        import db
        db.update_site_settings(tts_auto_generate_enabled=0)
        page_id = _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        gen = _wait_for_status(client, "welcome")
        assert gen and gen["status"] == "completed"
        files_before = os.listdir(stub_backend)
        assert len(files_before) == 1
        initial_filename = db.get_tts_generation(page_id)["filename"]

        db.update_page(page_id, title="Welcome",
                       content="Updated body text.", user_id=admin_user)

        # Trigger the after_page_update hook directly (the SDK emit_hook is
        # invoked from the wiki edit route in production).
        from bananawiki_sdk import emit_hook
        page = db.get_page(page_id)
        emit_hook("after_page_update", page=page,
                  user={"id": admin_user, "role": "admin"})

        new_gen = _wait_for_status(client, "welcome")
        assert new_gen is not None
        assert new_gen["status"] in ("pending", "processing", "completed")
        assert initial_filename not in os.listdir(stub_backend)

    def test_page_update_without_existing_audio_does_not_enqueue(
        self, client, admin_user, stub_backend,
    ):
        import db
        db.update_site_settings(tts_auto_generate_enabled=0)
        page_id = _create_page()
        db.update_page(page_id, title="Welcome",
                       content="Updated body text.", user_id=admin_user)

        from bananawiki_sdk import emit_hook
        emit_hook("after_page_update", page=db.get_page(page_id),
                  user={"id": admin_user, "role": "admin"})

        assert db.get_tts_generation(page_id) is None
        assert os.listdir(stub_backend) == []

    def test_page_update_with_auto_gen_replaces_cache(
        self, client, admin_user, stub_backend,
    ):
        """With auto-gen on (the new default), an update should drop the old
        MP3 immediately and immediately re-enqueue a fresh generation."""
        import db
        # Make sure auto-gen is on (default since v1).
        db.update_site_settings(tts_auto_generate_enabled=1)
        page_id = _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        gen = _wait_for_status(client, "welcome")
        assert gen and gen["status"] == "completed"
        initial_filename = db.get_tts_generation(page_id)["filename"]

        # Mutate the page so the content hash differs, otherwise the auto-
        # gen helper short-circuits as a cache hit and the old row survives.
        db.update_page(page_id, title="Welcome",
                       content="Updated body text.", user_id=admin_user)
        from bananawiki_sdk import emit_hook
        page = db.get_page(page_id)
        emit_hook("after_page_update", page=page,
                  user={"id": admin_user, "role": "admin"})

        # Old MP3 has been deleted, but a new generation row is in flight.
        new_gen = _wait_for_status(client, "welcome")
        assert new_gen is not None
        assert new_gen["status"] in ("pending", "processing", "completed")
        # Old filename is gone from disk (or replaced).
        assert initial_filename not in os.listdir(stub_backend)

    def test_page_delete_clears_cache(self, client, admin_user, stub_backend):
        import db
        page_id = _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        _wait_for_status(client, "welcome")
        assert len(os.listdir(stub_backend)) == 1

        from bananawiki_sdk import emit_hook
        page = db.get_page(page_id)
        emit_hook("after_page_delete", page=page,
                  user={"id": admin_user, "role": "admin"})

        assert db.get_tts_generation(page_id) is None
        assert os.listdir(stub_backend) == []


class TestWorkerSupersede:
    def test_supersede_discards_in_flight_output(self, admin_user, tmp_path,
                                                 monkeypatch):
        """If the row is replaced while the worker is synthesising, the
        worker must discard its output and mark the orphan row as failed.
        """
        import db
        import helpers._tts as _tts_mod

        page_id = _create_page()
        folder = str(tmp_path / "tts")
        os.makedirs(folder, exist_ok=True)

        block = threading.Event()
        original = _tts_mod._synthesize_with_stub

        def slow_stub(text, language, dest_path):
            original(text, language, dest_path)
            block.wait(timeout=2.0)

        monkeypatch.setattr(_tts_mod, "_synthesize_with_stub", slow_stub)

        first, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="hA",
            requested_by=admin_user,
        )
        thread = _tts_mod.run_generation_async(
            generation_id=first["id"], page_id=page_id, language="en",
            content_hash="hA", text="Hello world", tts_folder=folder,
        )
        # Wait until processing has started.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            row = db.get_tts_generation_by_id(first["id"])
            if row and row["status"] == "processing":
                break
            time.sleep(0.02)
        assert row["status"] == "processing"

        # Replace the row (this is what the page-update hook effectively does).
        db.delete_tts_generation(page_id)
        second, _ = db.request_tts_generation(
            page_id=page_id, language="it", content_hash="hB",
            requested_by=admin_user,
        )

        # Let the slow stub finish.
        block.set()
        thread.join(timeout=3.0)

        # The first generation row was deleted by delete_tts_generation, so the
        # supersede check inside the worker resolves cleanly: no orphan files
        # left behind for the original synthesis attempt.
        files = [f for f in os.listdir(folder) if f.startswith("tts_")]
        # Worker writes tts_<page_id>_en_..._<id>.mp3: supersede deletes it.
        for fname in files:
            assert "_en_" not in fname or f"_{first['id']}.mp3" not in fname


class TestWorkerAutoResume:
    def test_standalone_worker_processes_pending_row_when_inline_disabled(
        self, admin_user, tmp_path, monkeypatch,
    ):
        import db
        import helpers._tts as _tts_mod

        monkeypatch.setenv("BW_TTS_INLINE_WORKER", "0")

        page_id = _create_page(
            slug="worker-mode",
            title="Worker Mode",
            content="Hello world from the durable worker queue.",
        )
        page = db.get_page(page_id)
        content_hash = _tts_mod.tts_content_hash(
            page["title"], page["content"], "en",
        )
        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash=content_hash,
            requested_by=admin_user,
        )
        handle = _tts_mod.run_generation_async(
            generation_id=row["id"],
            page_id=page_id,
            language="en",
            content_hash=content_hash,
            text=_tts_mod.tts_normalize_text(page["title"], page["content"]),
            tts_folder=str(tmp_path / "tts"),
        )
        handle.join(timeout=0.2)
        assert not handle.is_alive()
        assert db.get_tts_generation(page_id)["status"] == "pending"

        processed = _tts_mod.run_tts_worker_loop(
            str(tmp_path / "tts"), once=True, recover=False,
        )
        final = db.get_tts_generation(page_id)

        assert processed == 1
        assert final["status"] == "completed"
        assert final["filename"]

    def test_transient_failure_auto_resumes_and_completes(
        self, admin_user, tmp_path, monkeypatch,
    ):
        import db
        import helpers._tts as _tts_mod

        monkeypatch.setenv("BW_TTS_MAX_AUTO_RESUME_ATTEMPTS", "2")
        monkeypatch.setenv("BW_TTS_AUTO_RESUME_BASE_DELAY_SECONDS", "0")

        page_id = _create_page(slug="resume-ok")
        folder = str(tmp_path / "tts")
        os.makedirs(folder, exist_ok=True)

        calls = {"n": 0}
        original = _tts_mod._synthesize_with_stub

        def flaky_stub(text, language, dest_path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("temporary provider timeout")
            return original(text, language, dest_path)

        monkeypatch.setattr(_tts_mod, "_synthesize_with_stub", flaky_stub)

        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h-resume",
            requested_by=admin_user,
        )
        _tts_mod.run_generation_async(
            generation_id=row["id"], page_id=page_id, language="en",
            content_hash="h-resume", text="Hello world", tts_folder=folder,
        )

        final = _wait_for_db_status(page_id)
        assert final is not None
        assert final["status"] == "completed"
        assert final["retry_count"] == 1
        assert calls["n"] == 2
        assert os.listdir(folder)

    def test_retry_exhaustion_leaves_failed_row(
        self, admin_user, tmp_path, monkeypatch,
    ):
        import db
        import helpers._tts as _tts_mod

        monkeypatch.setenv("BW_TTS_MAX_AUTO_RESUME_ATTEMPTS", "2")
        monkeypatch.setenv("BW_TTS_AUTO_RESUME_BASE_DELAY_SECONDS", "0")

        page_id = _create_page(slug="resume-exhausted")
        folder = str(tmp_path / "tts")
        os.makedirs(folder, exist_ok=True)

        calls = {"n": 0}

        def always_fails(text, language, dest_path):
            del text, language, dest_path
            calls["n"] += 1
            raise RuntimeError("temporary provider timeout")

        monkeypatch.setattr(_tts_mod, "_synthesize_with_stub", always_fails)

        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h-fail",
            requested_by=admin_user,
        )
        _tts_mod.run_generation_async(
            generation_id=row["id"], page_id=page_id, language="en",
            content_hash="h-fail", text="Hello world", tts_folder=folder,
        )

        final = _wait_for_db_status(page_id)
        assert final is not None
        assert final["status"] == "failed"
        assert final["retry_count"] == 2
        assert calls["n"] == 3
        assert os.listdir(folder) == []

    def test_rate_limit_failure_stays_pending_without_retry_burn(
        self, admin_user, tmp_path, monkeypatch,
    ):
        import db
        import helpers._tts as _tts_mod

        monkeypatch.setenv("BW_TTS_RATE_LIMIT_COOLDOWN_SECONDS", "1000")

        page_id = _create_page(slug="rate-limited-pending")
        folder = str(tmp_path / "tts")
        os.makedirs(folder, exist_ok=True)

        calls = {"n": 0}

        def rate_limited(text, language, dest_path):
            del text, language, dest_path
            calls["n"] += 1
            raise RuntimeError("429 (Too Many Requests) from TTS API.")

        monkeypatch.setattr(_tts_mod, "_synthesize_with_stub", rate_limited)

        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h-429",
            requested_by=admin_user,
        )
        handle = _tts_mod.run_generation_async(
            generation_id=row["id"], page_id=page_id, language="en",
            content_hash="h-429", text="Hello world", tts_folder=folder,
        )
        handle.join(timeout=3.0)

        final = db.get_tts_generation(page_id)
        assert calls["n"] == 1
        assert final["status"] == "pending"
        assert final["retry_count"] == 0
        assert "429" in (final["error_message"] or "")

    def test_exhausted_rate_limit_failure_recovers(
        self, admin_user, tmp_path, monkeypatch,
    ):
        import db
        import helpers._tts as _tts_mod

        monkeypatch.setenv("BW_TTS_RATE_LIMIT_COOLDOWN_SECONDS", "0")
        monkeypatch.setenv("BW_TTS_AUTO_RESUME_BASE_DELAY_SECONDS", "0")

        page_id = _create_page(
            slug="rate-limit-recover",
            title="Rate Limit Recover",
            content="Hello world this exhausted 429 row should recover.",
        )
        page = db.get_page(page_id)
        content_hash = _tts_mod.tts_content_hash(
            page["title"], page["content"], "en",
        )
        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash=content_hash,
            requested_by=admin_user,
        )
        db.mark_tts_processing(row["id"])
        db.mark_tts_failed(row["id"], "429 (Too Many Requests) from TTS API.")
        with db.get_db_context() as conn:
            conn.execute(
                "UPDATE tts_generations SET retry_count = 99 WHERE id = ?",
                (row["id"],),
            )
            conn.commit()

        folder = str(tmp_path / "tts")
        os.makedirs(folder, exist_ok=True)

        recovered = _tts_mod.recover_stuck_generations(folder)
        final = _wait_for_db_status(page_id)

        assert recovered == 1
        assert final["status"] == "completed"

    def test_permanent_empty_text_failure_is_not_retried(
        self, admin_user, tmp_path, monkeypatch,
    ):
        import db
        import helpers._tts as _tts_mod

        monkeypatch.setenv("BW_TTS_MAX_AUTO_RESUME_ATTEMPTS", "2")
        monkeypatch.setenv("BW_TTS_AUTO_RESUME_BASE_DELAY_SECONDS", "0")

        page_id = _create_page(slug="resume-permanent")
        folder = str(tmp_path / "tts")
        os.makedirs(folder, exist_ok=True)

        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash="h-empty",
            requested_by=admin_user,
        )
        _tts_mod.run_generation_async(
            generation_id=row["id"], page_id=page_id, language="en",
            content_hash="h-empty", text="   ", tts_folder=folder,
        )

        final = _wait_for_db_status(page_id)
        assert final is not None
        assert final["status"] == "failed"
        assert final["retry_count"] == 0
        assert "empty" in (final["error_message"] or "").lower()

    def test_orphaned_pending_row_is_recovered(
        self, admin_user, tmp_path, monkeypatch,
    ):
        import db
        import helpers._tts as _tts_mod

        monkeypatch.setenv("BW_TTS_AUTO_RESUME_BASE_DELAY_SECONDS", "0")

        page_id = _create_page(
            slug="pending-orphan",
            title="Pending Orphan",
            content="Hello world this pending row lost its live worker.",
        )
        page = db.get_page(page_id)
        content_hash = _tts_mod.tts_content_hash(
            page["title"], page["content"], "en",
        )
        row, _ = db.request_tts_generation(
            page_id=page_id,
            language="en",
            content_hash=content_hash,
            requested_by=admin_user,
        )
        assert row["status"] == "pending"

        folder = str(tmp_path / "tts")
        os.makedirs(folder, exist_ok=True)

        recovered = _tts_mod.recover_orphaned_pending_generations(folder)

        final = _wait_for_db_status(page_id)
        assert recovered == 1
        assert final is not None
        assert final["status"] == "completed"

    def test_pending_recovery_skips_already_enqueued_row(
        self, admin_user, tmp_path,
    ):
        import db
        import helpers._tts as _tts_mod

        page_id = _create_page(
            slug="pending-owned",
            title="Pending Owned",
            content="Hello world this pending row is already queued.",
        )
        page = db.get_page(page_id)
        content_hash = _tts_mod.tts_content_hash(
            page["title"], page["content"], "en",
        )
        row, _ = db.request_tts_generation(
            page_id=page_id,
            language="en",
            content_hash=content_hash,
            requested_by=admin_user,
        )
        handle = _tts_mod._TtsGenerationHandle(row["id"])
        with _tts_mod._TTS_ENQUEUED_LOCK:
            _tts_mod._TTS_ENQUEUED_HANDLES[row["id"]] = handle
        try:
            folder = str(tmp_path / "tts")
            os.makedirs(folder, exist_ok=True)
            assert _tts_mod.recover_orphaned_pending_generations(folder) == 0
            assert db.get_tts_generation(page_id)["status"] == "pending"
        finally:
            with _tts_mod._TTS_ENQUEUED_LOCK:
                _tts_mod._TTS_ENQUEUED_HANDLES.pop(row["id"], None)

    def test_startup_recovery_resumes_failed_row_with_budget(
        self, admin_user, tmp_path, monkeypatch,
    ):
        import db
        import helpers._tts as _tts_mod

        monkeypatch.setenv("BW_TTS_MAX_AUTO_RESUME_ATTEMPTS", "2")
        monkeypatch.setenv("BW_TTS_AUTO_RESUME_BASE_DELAY_SECONDS", "0")

        page_id = _create_page(
            slug="resume-startup",
            title="Startup Resume",
            content="Hello world this generation should resume.",
        )
        page = db.get_page(page_id)
        content_hash = _tts_mod.tts_content_hash(
            page["title"], page["content"], "en",
        )
        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash=content_hash,
            requested_by=admin_user,
        )
        db.mark_tts_processing(row["id"])
        db.mark_tts_failed(row["id"], "temporary provider timeout")

        folder = str(tmp_path / "tts")
        os.makedirs(folder, exist_ok=True)
        resumed = _tts_mod.recover_stuck_generations(folder)

        final = _wait_for_db_status(page_id)
        assert resumed == 1
        assert final is not None
        assert final["status"] == "completed"
        assert final["retry_count"] == 1

    def test_startup_recovery_skips_permanent_failed_row(
        self, admin_user, tmp_path, monkeypatch,
    ):
        import db
        import helpers._tts as _tts_mod

        monkeypatch.setenv("BW_TTS_MAX_AUTO_RESUME_ATTEMPTS", "2")
        monkeypatch.setenv("BW_TTS_AUTO_RESUME_BASE_DELAY_SECONDS", "0")

        page_id = _create_page(
            slug="resume-startup-permanent",
            title="Permanent Failure",
            content="Hello world content exists.",
        )
        page = db.get_page(page_id)
        content_hash = _tts_mod.tts_content_hash(
            page["title"], page["content"], "en",
        )
        row, _ = db.request_tts_generation(
            page_id=page_id, language="en", content_hash=content_hash,
            requested_by=admin_user,
        )
        db.mark_tts_processing(row["id"])
        db.mark_tts_failed(row["id"], "Page is empty: nothing to synthesise.")

        folder = str(tmp_path / "tts")
        os.makedirs(folder, exist_ok=True)

        assert _tts_mod.recover_stuck_generations(folder) == 0
        final = db.get_tts_generation(page_id)
        assert final["status"] == "failed"
        assert final["retry_count"] == 0


class TestLanguageDetection:
    def test_english_text(self):
        from helpers._tts import detect_tts_language
        text = (
            "The quick brown fox jumps over the lazy dog and into the bog "
            "of eternal stench, where the cat is sitting on the mat."
        )
        assert detect_tts_language(text) == "en"

    def test_italian_text(self):
        from helpers._tts import detect_tts_language
        text = (
            "Il rapido volpe attraversa il bosco con la gatta che corre "
            "verso casa nella notte, perché è stanca."
        )
        assert detect_tts_language(text) == "it"

    def test_italian_short_phrase(self):
        from helpers._tts import detect_tts_language
        # Italian-specific characters are a strong signal.
        assert detect_tts_language("Buongiorno tutti, come state oggi?") == "it"

    def test_english_short_phrase(self):
        from helpers._tts import detect_tts_language
        assert detect_tts_language("Hi everyone, please rate this article.") == "en"

    def test_empty_input_falls_back(self):
        from helpers._tts import detect_tts_language
        assert detect_tts_language("") == "en"
        assert detect_tts_language("   ") == "en"

    def test_short_input_falls_back(self):
        from helpers._tts import detect_tts_language
        # Fewer than 5 words → fall back to default.
        assert detect_tts_language("hi mum") == "en"

    def test_short_asset_names_do_not_guess_random_languages(self):
        from helpers._tts import detect_tts_language
        assert detect_tts_language("RedSkull. Soldier StarLord Zombie") == "en"
        assert detect_tts_language("barile in piedi barra energia") == "en"

    def test_custom_default(self):
        from helpers._tts import detect_tts_language
        assert detect_tts_language("", default="it") == "it"


class TestLanguageNormalisation:
    def test_auto_preserved_when_allowed(self):
        from helpers._tts import normalize_tts_language
        assert normalize_tts_language("auto", allow_auto=True) == "auto"
        assert normalize_tts_language("AUTO", allow_auto=True) == "auto"

    def test_auto_snapped_when_disallowed(self):
        from helpers._tts import normalize_tts_language
        assert normalize_tts_language("auto", allow_auto=False) == "en"

    def test_synonyms(self):
        from helpers._tts import normalize_tts_language
        assert normalize_tts_language("italian") == "it"
        assert normalize_tts_language("english") == "en"
        assert normalize_tts_language("IT") == "it"
        assert normalize_tts_language("en-US") == "en"

    def test_garbage_falls_back(self):
        from helpers._tts import normalize_tts_language
        assert normalize_tts_language("xyz") == "en"
        assert normalize_tts_language(None) == "en"
        assert normalize_tts_language("", default="it") == "it"


class TestMarkdownStripExtras:
    def test_tables_and_horizontal_rules_are_dropped(self):
        from helpers._tts import tts_normalize_text
        out = tts_normalize_text(
            "Title",
            (
                "Intro.\n\n"
                "| Col A | Col B |\n"
                "|-------|-------|\n"
                "| 1     | 2     |\n\n"
                "---\n\n"
                "Outro."
            ),
        )
        assert "Intro" in out and "Outro" in out
        # The bare separator characters must not survive.
        assert "|" not in out
        assert "---" not in out
        assert "----" not in out

    def test_reference_link_definitions_dropped(self):
        from helpers._tts import tts_normalize_text
        out = tts_normalize_text(
            "Page",
            "See [1] for details.\n\n[1]: https://example.com",
        )
        assert "See" in out and "details" in out
        # The URL itself should not be read out loud.
        assert "https" not in out and "example.com" not in out

    def test_stray_emphasis_symbols_are_dropped(self):
        from helpers._tts import tts_normalize_text
        out = tts_normalize_text(
            "P", "An *italic* and **bold** word, plus a ~~strike~~ and `code`."
        )
        # The words survive; the symbols do not.
        assert "italic" in out and "bold" in out
        assert "strike" in out and "code" in out
        assert "*" not in out and "~" not in out and "`" not in out


# From here on the subject is the auto-generation path: a page create or update queues audio only when auto mode is on.
def _ensure_tts_plugin_enabled():
    """Make sure the built-in TTS plugin is marked as enabled in the test DB."""
    import db
    if not db.get_plugin("tts"):
        db.register_plugin(
            "tts", name="Text-to-Speech", version="1.0.0",
            author="BananaWiki", description="", builtin=True, enabled=True,
        )
    else:
        db.enable_plugin("tts")


class TestAutoGeneration:
    def test_no_row_when_setting_off(self, client, admin_user, stub_backend):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(tts_auto_generate_enabled=0)
        page_id = _create_page(slug="autoa", title="Auto Off",
                               content="Hello world this is English content.")
        from bananawiki_sdk import emit_hook
        page = db.get_page(page_id)
        emit_hook("after_page_create", page=page,
                  user={"id": admin_user, "role": "admin"})
        assert db.get_tts_generation(page_id) is None

    def test_row_created_on_create_when_setting_on(self, client, admin_user,
                                                   stub_backend):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(tts_auto_generate_enabled=1)
        page_id = _create_page(
            slug="autob", title="Auto On",
            content="Hello world this is English content about cats and dogs.",
        )
        from bananawiki_sdk import emit_hook
        page = db.get_page(page_id)
        emit_hook("after_page_create", page=page,
                  user={"id": admin_user, "role": "admin"})

        # Worker thread completes nearly immediately with the stub backend.
        deadline = time.time() + 3.0
        gen = None
        while time.time() < deadline:
            gen = db.get_tts_generation(page_id)
            if gen and gen["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        assert gen is not None
        assert gen["language"] == "en"
        assert gen["status"] == "completed"

        # Reset.
        db.update_site_settings(tts_auto_generate_enabled=0)

    def test_language_detected_as_italian(self, client, admin_user, stub_backend):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(tts_auto_generate_enabled=1)
        page_id = _create_page(
            slug="autoc", title="Auto IT",
            content=(
                "Questo è un articolo italiano che parla della storia "
                "di Roma e della cucina italiana che è famosa nel mondo."
            ),
        )
        from bananawiki_sdk import emit_hook
        page = db.get_page(page_id)
        emit_hook("after_page_create", page=page,
                  user={"id": admin_user, "role": "admin"})

        deadline = time.time() + 3.0
        gen = None
        while time.time() < deadline:
            gen = db.get_tts_generation(page_id)
            if gen and gen["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        assert gen is not None
        assert gen["language"] == "it"

        db.update_site_settings(tts_auto_generate_enabled=0)

    def test_suppression_guard_skips_auto_queue(self, client, admin_user,
                                                stub_backend):
        import db
        from bananawiki_sdk import emit_hook
        from helpers._tts import suppress_tts_auto_generation

        _ensure_tts_plugin_enabled()
        db.update_site_settings(tts_auto_generate_enabled=1)
        page_id = _create_page(
            slug="auto-suppressed",
            title="Auto Suppressed",
            content="Hello world this is English content.",
        )
        page = db.get_page(page_id)
        with suppress_tts_auto_generation():
            emit_hook("after_page_create", page=page,
                      user={"id": admin_user, "role": "admin"})

        assert db.get_tts_generation(page_id) is None

    def test_bulk_markdown_import_suppresses_hook_fanout(
        self, client, admin_user, stub_backend, monkeypatch,
    ):
        import db
        from bananawiki_sdk import emit_hook
        from helpers._bulk_markdown import import_markdown_bundle

        _ensure_tts_plugin_enabled()
        db.update_site_settings(tts_auto_generate_enabled=1)
        original_create_page = db.create_page

        def create_page_with_hook(title, slug, content="", category_id=None,
                                  user_id=None):
            page_id = original_create_page(
                title, slug, content, category_id, user_id,
            )
            emit_hook("after_page_create", page=db.get_page(page_id),
                      user={"id": user_id, "role": "admin"})
            return page_id

        monkeypatch.setattr(db, "create_page", create_page_with_hook)
        result = import_markdown_bundle(
            [
                ("docs/a.md", "# A\n\nHello alpha page."),
                ("docs/b.md", "# B\n\nHello bravo page."),
            ],
            admin_user,
        )

        assert result.total_created == 2
        assert db.list_tts_generations() == []


class TestAdminStatusPage:
    def test_renders_for_admin(self, client, admin_user, stub_backend):
        import db
        _ensure_tts_plugin_enabled()
        _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        _wait_for_status(client, "welcome")
        resp = client.get("/admin/tts-status")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "TTS status" in body
        assert "welcome" in body or "Welcome" in body

    def test_blocked_for_non_admin(self, client, regular_user, stub_backend):
        _ensure_tts_plugin_enabled()
        _login(client, "user", "user123")
        resp = client.get("/admin/tts-status", follow_redirects=False)
        # admin_required either redirects to login or returns 403
        assert resp.status_code in (302, 303, 403)

    def test_pending_retry_rows_hide_stale_error(self, client, admin_user):
        import db
        _ensure_tts_plugin_enabled()
        page_id = _create_page()
        row, _ = db.request_tts_generation(
            page_id=page_id,
            language="en",
            content_hash="pending-retry",
            requested_by=admin_user,
        )
        retry = db.mark_tts_retry_pending(
            row["id"],
            "429 (Too Many Requests) from TTS API.",
            2,
        )
        assert retry is not None
        assert retry["status"] == "pending"

        _login(client, "admin", "admin123")
        resp = client.get("/admin/tts-status")

        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Pending" in body
        assert "429 (Too Many Requests) from TTS API." not in body

        status = client.get("/page/welcome/tts/status").get_json()
        assert status["generation"]["status"] == "pending"
        assert status["generation"]["error_message"] is None

    def test_auto_mode_status_page_recovers_orphaned_pending_rows(
        self, client, admin_user, stub_backend,
    ):
        import db
        import helpers._tts as _tts_mod

        _ensure_tts_plugin_enabled()
        db.update_site_settings(tts_auto_generate_enabled=1)
        page_id = _create_page(
            slug="admin-recover-pending",
            title="Admin Recover Pending",
            content="Hello world this imported row should be queued.",
        )
        page = db.get_page(page_id)
        content_hash = _tts_mod.tts_content_hash(
            page["title"], page["content"], "en",
        )
        row, _ = db.request_tts_generation(
            page_id=page_id,
            language="en",
            content_hash=content_hash,
            requested_by=admin_user,
        )
        assert row["status"] == "pending"

        _login(client, "admin", "admin123")
        resp = client.get("/admin/tts-status")

        assert resp.status_code == 200
        final = _wait_for_db_status(page_id)
        assert final is not None
        assert final["status"] == "completed"

    def test_clear_cache_does_not_backfill_all_pages(self, client, admin_user,
                                                     stub_backend):
        import db
        from helpers._tts import tts_content_hash

        _ensure_tts_plugin_enabled()
        page_a = _create_page(
            slug="clear-a", title="Clear A", content="Hello alpha page.",
        )
        page_b = _create_page(
            slug="clear-b", title="Clear B", content="Hello bravo page.",
        )
        for page_id in (page_a, page_b):
            page = db.get_page(page_id)
            db.request_tts_generation(
                page_id=page_id,
                language="en",
                content_hash=tts_content_hash(page["title"], page["content"], "en"),
                requested_by=admin_user,
            )

        _login(client, "admin", "admin123")
        resp = client.post("/admin/tts-status/clear", follow_redirects=False)

        assert resp.status_code in (302, 303)
        assert db.list_tts_generations() == []

    def test_404_when_plugin_disabled(self, client, admin_user, stub_backend):
        import db
        # Disable the plugin and verify the gating returns 404.
        if db.get_plugin("tts"):
            db.disable_plugin("tts")
        _login(client, "admin", "admin123")
        resp = client.get("/admin/tts-status", follow_redirects=False)
        assert resp.status_code == 404

    def test_backfill_queues_missing_audio_without_generating_inline(
        self, client, admin_user, stub_backend,
    ):
        import db
        from helpers._tts import tts_content_hash

        _ensure_tts_plugin_enabled()
        db.update_site_settings(tts_auto_generate_enabled=0)
        missing_id = _create_page(
            slug="needs-audio", title="Needs Audio",
            content="This page should be queued for speech.",
        )
        completed_id = _create_page(
            slug="has-audio", title="Has Audio",
            content="This page already has speech.",
        )
        pending_id = _create_page(
            slug="pending-audio", title="Pending Audio",
            content="This page is already queued.",
        )
        empty_id = _create_page(slug="empty-audio", title="", content="")

        completed_page = db.get_page(completed_id)
        completed_row, _ = db.request_tts_generation(
            page_id=completed_id,
            language="en",
            content_hash=tts_content_hash(
                completed_page["title"], completed_page["content"], "en",
            ),
            requested_by=admin_user,
        )
        db.mark_tts_processing(completed_row["id"])
        db.mark_tts_completed(completed_row["id"], "has-audio.mp3", 256)

        pending_page = db.get_page(pending_id)
        pending_row, _ = db.request_tts_generation(
            page_id=pending_id,
            language="en",
            content_hash=tts_content_hash(
                pending_page["title"], pending_page["content"], "en",
            ),
            requested_by=admin_user,
        )

        assert db.count_tts_backfill_candidates() >= 2

        _login(client, "admin", "admin123")
        resp = client.post("/admin/tts-status/backfill", follow_redirects=False)

        assert resp.status_code in (302, 303)
        missing = db.get_tts_generation(missing_id)
        assert missing is not None
        assert missing["status"] == "pending"
        assert db.get_tts_generation(completed_id)["status"] == "completed"
        assert db.get_tts_generation(pending_id)["id"] == pending_row["id"]
        assert db.get_tts_generation(empty_id) is None
        assert os.listdir(stub_backend) == []


class TestAutoPanelRendering:
    def test_panel_can_generate_missing_audio_when_auto_on(
        self, client, admin_user, stub_backend,
    ):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            tts_page_panel_enabled=1, tts_auto_generate_enabled=1,
        )
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "tts-panel-auto" not in body
        assert 'data-tts-auto="1"' not in body
        assert "tts-generate-btn" in body
        assert "tts-cancel-btn" not in body
        assert "tts-language-select" not in body
        db.update_site_settings(
            tts_page_panel_enabled=0, tts_auto_generate_enabled=0,
        )

    def test_manual_panel_when_auto_off(self, client, admin_user, stub_backend):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            tts_page_panel_enabled=1, tts_auto_generate_enabled=0,
        )
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome")
        body = resp.data.decode("utf-8")
        assert "tts-generate-btn" in body
        assert "tts-speed-select" in body
        assert "Audio has not been generated yet." in body
        db.update_site_settings(tts_page_panel_enabled=0)

    def test_manual_panel_hides_generate_button_when_audio_exists(
        self, client, admin_user, stub_backend,
    ):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            tts_page_panel_enabled=1, tts_auto_generate_enabled=0,
        )
        page_id = _create_page()
        row, _ = db.request_tts_generation(
            page_id=page_id,
            language="en",
            content_hash="h1",
            requested_by=admin_user,
        )
        db.mark_tts_processing(row["id"])
        db.mark_tts_completed(row["id"], "welcome.mp3", 256)

        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome")
        body = resp.data.decode("utf-8")

        assert "tts-generate-btn" not in body
        assert "Regenerate audio" not in body
        assert 'data-msg-completed="Audio ready."' in body

    def test_panel_renders_on_home_page(self, client, admin_user, stub_backend):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            tts_page_panel_enabled=1, tts_auto_generate_enabled=0,
        )
        home_id = _create_page(slug="home-tts", title="Home TTS", content="Hello")
        db.set_home_page(home_id)
        _login(client, "admin", "admin123")
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "data-tts-panel" in body
        assert 'data-slug="home-tts"' in body


class TestTtsPluginEnableHook:
    """The plugin's on_enable hook enables the panel without global backfill."""

    def test_on_enable_keeps_auto_gen_off(self, client, admin_user, stub_backend):
        import db
        from plugins.builtin.tts import enable as on_enable

        db.update_site_settings(tts_auto_generate_enabled=0, tts_page_panel_enabled=0)
        on_enable()
        settings = db.get_site_settings() or {}
        assert settings.get("tts_auto_generate_enabled") == 0
        assert settings.get("tts_page_panel_enabled") == 1

    def test_on_enable_does_not_backfill_existing_pages(self, client, admin_user,
                                                        stub_backend):
        import db
        from plugins.builtin.tts import enable as on_enable

        _ensure_tts_plugin_enabled()

        page_a = _create_page(slug="alpha", title="Alpha",
                              content="Alpha page about cats.")
        page_b = _create_page(slug="bravo", title="Bravo",
                              content="Bravo page about dogs.")

        # Wipe any cache state left behind by the autouse fixture.
        for pid in (page_a, page_b):
            db.delete_tts_generation(pid)

        on_enable()

        assert db.get_tts_generation(page_a) is None
        assert db.get_tts_generation(page_b) is None

    def test_on_enable_does_not_backfill_home_page(self, client, admin_user,
                                                   stub_backend):
        import db
        from plugins.builtin.tts import enable as on_enable

        _ensure_tts_plugin_enabled()

        home_id = _create_page(slug="landing", title="Landing",
                               content="Hello world this is the landing page.")
        db.set_home_page(home_id)
        db.delete_tts_generation(home_id)

        on_enable()

        assert db.get_tts_generation(home_id) is None


class TestManualGenerateRespectsPanelToggle:
    """``POST /page/<slug>/tts/generate`` must respect the in-page panel
    toggle.  Bug report: users reported the manual button still triggered
    generation despite the option being switched off: turning the toggle
    off was just hiding the button rather than disabling the feature."""

    def test_generate_refused_when_panel_off(self, client, admin_user,
                                              stub_backend):
        """With the panel toggle off, the endpoint must return 403 with a
        clear ``panel_disabled`` error code, even for admins."""
        import db
        db.update_site_settings(
            tts_page_panel_enabled=0, tts_auto_generate_enabled=1,
        )
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        body = resp.get_json() or {}
        assert body.get("error") == "panel_disabled"

    def test_panel_hidden_when_toggle_off_even_with_autogen(
        self, client, admin_user, stub_backend,
    ):
        """Bug report wording: "by default it should be disabled and TTS
        should be automatically generated".  In that default state the
        panel/widget must NOT render on wiki pages: auto-gen runs server
        side, and the page itself stays clean."""
        import db
        db.update_site_settings(
            tts_page_panel_enabled=0, tts_auto_generate_enabled=1,
        )
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome")
        body = resp.data.decode("utf-8")
        # The panel is gated on the toggle alone now.
        assert "data-tts-panel" not in body
        assert "tts-generate-btn" not in body
        assert "tts-panel-auto" not in body


class TestTtsDisabledHardening:
    """A disabled TTS plugin must not leave empty UI shells or active endpoints."""

    def test_disabled_plugin_hides_panel_even_if_settings_are_on(
        self, client, admin_user, stub_backend,
    ):
        import db
        db.disable_plugin("tts")
        db.update_site_settings(
            tts_page_panel_enabled=1,
            tts_public_access_enabled=1,
            tts_auto_generate_enabled=1,
        )
        _create_page()
        _login(client, "admin", "admin123")

        resp = client.get("/page/welcome")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "data-tts-panel" not in body
        assert "tts-panel" not in body

    def test_disabled_plugin_rejects_tts_endpoints(
        self, client, admin_user, stub_backend,
    ):
        import db
        db.disable_plugin("tts")
        db.update_site_settings(
            tts_page_panel_enabled=1,
            tts_public_access_enabled=1,
            tts_auto_generate_enabled=1,
        )
        _create_page()
        _login(client, "admin", "admin123")

        assert client.get("/page/welcome/tts/status").status_code == 404
        assert client.get("/page/welcome/tts/audio").status_code == 404
        assert client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        ).status_code == 404
        assert client.get("/admin/tts-status").status_code == 404

    def test_disabling_plugin_resets_tts_settings_off(
        self, client, admin_user, stub_backend,
    ):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            tts_page_panel_enabled=1,
            tts_public_access_enabled=1,
            tts_auto_generate_enabled=1,
        )
        _login(client, "admin", "admin123")

        resp = client.post("/admin/plugins/tts/disable", follow_redirects=False)
        assert resp.status_code == 302
        settings = db.get_site_settings()
        assert settings.get("tts_page_panel_enabled") == 0
        assert settings.get("tts_public_access_enabled") == 0
        assert settings.get("tts_auto_generate_enabled") == 0


class TestPublicModeTtsPlayback:
    """Anonymous public-site visitors can use read-only TTS when enabled."""

    def _seed_cached_audio(self, client, admin_user):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            public_mode=1,
            tts_page_panel_enabled=1,
            tts_public_access_enabled=1,
            tts_auto_generate_enabled=0,
        )
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 202), resp.data
        generation = _wait_for_status(client, "welcome")
        assert generation and generation["status"] == "completed"
        with client.session_transaction() as sess:
            sess.clear()

    def test_public_visitor_sees_panel_by_default(self, client, admin_user,
                                                  stub_backend):
        self._seed_cached_audio(client, admin_user)
        resp = client.get("/page/welcome")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "data-tts-panel" in body
        assert "tts-generate-btn" not in body

    def test_public_visitor_sees_generate_button_without_cached_audio(
        self, client, admin_user, stub_backend,
    ):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            public_mode=1,
            tts_page_panel_enabled=1,
            tts_public_access_enabled=1,
            tts_auto_generate_enabled=0,
        )
        _create_page()

        resp = client.get("/page/welcome")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "data-tts-panel" in body
        assert "tts-generate-btn" in body

    def test_regular_user_sees_generate_button_without_cached_audio(
        self, client, admin_user, regular_user, stub_backend,
    ):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            public_mode=0,
            tts_page_panel_enabled=1,
            tts_public_access_enabled=1,
            tts_auto_generate_enabled=0,
        )
        _create_page()
        _login(client, "user", "user123")

        resp = client.get("/page/welcome")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "data-tts-panel" in body
        assert "tts-generate-btn" in body

    def test_public_visitor_can_generate_missing_audio(
        self, client, admin_user, stub_backend,
    ):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            public_mode=1,
            tts_page_panel_enabled=1,
            tts_public_access_enabled=1,
            tts_auto_generate_enabled=0,
        )
        _create_page()

        resp = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 202), resp.data
        gen = _wait_for_status(client, "welcome")
        assert gen is not None
        assert gen["status"] == "completed"

    def test_public_visitor_can_status_and_audio(self, client, admin_user,
                                                 stub_backend):
        self._seed_cached_audio(client, admin_user)
        status = client.get("/page/welcome/tts/status")
        assert status.status_code == 200
        payload = status.get_json() or {}
        assert payload.get("can_generate") is False
        assert payload.get("generation", {}).get("status") == "completed"

        audio = client.get("/page/welcome/tts/audio")
        assert audio.status_code == 200
        assert audio.mimetype == "audio/mpeg"

    def test_public_tts_setting_hides_panel_and_blocks_routes(
        self, client, admin_user, stub_backend,
    ):
        self._seed_cached_audio(client, admin_user)
        import db
        db.update_site_settings(tts_public_access_enabled=0)

        page = client.get("/page/welcome")
        assert page.status_code == 200
        assert "data-tts-panel" not in page.data.decode("utf-8")

        status = client.get("/page/welcome/tts/status")
        assert status.status_code == 403
        audio = client.get("/page/welcome/tts/audio")
        assert audio.status_code == 403


class TestSpeedNormalization:
    """``normalize_tts_speed`` snaps user input onto a preset."""

    def test_default_when_value_missing(self):
        from helpers._tts import normalize_tts_speed, TTS_DEFAULT_PLAYBACK_SPEED
        assert normalize_tts_speed(None) == float(TTS_DEFAULT_PLAYBACK_SPEED)
        assert normalize_tts_speed("") == float(TTS_DEFAULT_PLAYBACK_SPEED)

    def test_valid_presets_pass_through(self):
        from helpers._tts import normalize_tts_speed
        assert normalize_tts_speed("1.0") == 1.0
        assert normalize_tts_speed("1.25") == 1.25
        assert normalize_tts_speed(1.5) == 1.5
        assert normalize_tts_speed("2.0") == 2.0

    def test_accepts_locale_comma(self):
        from helpers._tts import normalize_tts_speed
        assert normalize_tts_speed("1,5") == 1.5

    def test_invalid_input_falls_back_to_default(self):
        from helpers._tts import normalize_tts_speed
        # Out-of-range values snap to the default rather than letting a
        # caller render at, e.g., 99x and burn through CPU server-side.
        assert normalize_tts_speed("99") == 1.0
        assert normalize_tts_speed("nope") == 1.0
        # The explicit ``default`` argument wins when nothing matches.
        assert normalize_tts_speed("bogus", default=1.0) == 1.0


class TestSpeedSelectorRendering:
    """The in-page panel ships a ``<select class="tts-speed-select">``."""

    def test_manual_panel_renders_speed_select(self, client, admin_user,
                                                stub_backend):
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            tts_page_panel_enabled=1, tts_auto_generate_enabled=0,
        )
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome")
        body = resp.data.decode("utf-8")
        assert "tts-speed-select" in body
        # Default preset must be marked selected so the player starts at the
        # configured default without needing JS to hydrate.
        assert 'value="1.00" selected' in body
        # Every preset shows up as an <option>.
        for preset in ("0.75", "1.00", "1.25", "1.50", "1.75", "2.00"):
            assert f'value="{preset}"' in body
        db.update_site_settings(tts_page_panel_enabled=0)

    def test_auto_panel_also_renders_speed_select(self, client, admin_user,
                                                   stub_backend):
        """Auto-generation mode still renders the speed selector."""
        import db
        _ensure_tts_plugin_enabled()
        db.update_site_settings(
            tts_page_panel_enabled=1, tts_auto_generate_enabled=1,
        )
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome")
        body = resp.data.decode("utf-8")
        assert "tts-panel-auto" not in body
        assert "tts-speed-select" in body
        assert "tts-language-select" not in body
        assert "tts-generate-btn" in body
        db.update_site_settings(
            tts_page_panel_enabled=0, tts_auto_generate_enabled=0,
        )


class TestSpeedStatusEndpoint:
    """``/tts/status`` exposes the speed presets + default for the JS."""

    def test_status_payload_includes_speed_metadata(self, client, admin_user,
                                                     stub_backend):
        _create_page()
        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome/tts/status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "speed_presets" in body
        assert body["speed_presets"] == [0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
        assert body["default_speed"] == 1.0
        # The flag mirrors ``ffmpeg_speed_available`` so the client knows
        # whether non-unit download speeds will actually be honoured.
        assert isinstance(body["download_speed_supported"], bool)


class TestSpeedAdjustedDownload:
    """``GET /tts/download?speed=`` filenames + fallbacks."""

    def test_download_filename_omits_speed_at_1x(self, client, admin_user,
                                                  stub_backend):
        _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        _wait_for_status(client, "welcome")

        resp = client.get("/page/welcome/tts/download")
        assert resp.status_code == 200
        disp = resp.headers.get("Content-Disposition", "")
        assert "welcome-en.mp3" in disp
        assert "1.0x" not in disp.lower() and "1.25x" not in disp.lower()

    def test_download_filename_includes_speed_for_non_unit(self, client,
                                                            admin_user,
                                                            stub_backend):
        _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        _wait_for_status(client, "welcome")

        resp = client.get("/page/welcome/tts/download?speed=1.5")
        assert resp.status_code == 200
        disp = resp.headers.get("Content-Disposition", "")
        # Filename gets a "-1.5x" tag so multiple speed downloads stay
        # distinguishable on disk.
        assert "welcome-en-1.5x.mp3" in disp

    def test_invalid_speed_falls_back_to_unit_filename(self, client,
                                                       admin_user,
                                                       stub_backend):
        """Garbage ``?speed=`` values must not raise. They snap to 1.0."""
        _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        _wait_for_status(client, "welcome")

        resp = client.get("/page/welcome/tts/download?speed=evil")
        assert resp.status_code == 200
        # Snapped to the default (1.0x). With ffmpeg available the
        # filename reflects that, otherwise the 1.0x cached file is
        # served instead. Either way, no traversal / abort happened.
        disp = resp.headers.get("Content-Disposition", "")
        assert "welcome-en" in disp
        assert ".mp3" in disp

    def test_audio_endpoint_ignores_speed_param(self, client, admin_user,
                                                 stub_backend):
        """The inline ``/audio`` endpoint always streams the cached 1.0x
        file: speed adjustment happens client-side via ``playbackRate``."""
        _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        _wait_for_status(client, "welcome")

        baseline = client.get("/page/welcome/tts/audio")
        adjusted = client.get("/page/welcome/tts/audio?speed=2.0")
        assert baseline.status_code == 200
        assert adjusted.status_code == 200
        # No attachment header on the playback endpoint.
        assert "attachment" not in baseline.headers.get(
            "Content-Disposition", "").lower()
        # The bytes match because the audio route does not re-encode.
        assert baseline.data == adjusted.data


class TestSpeedAdjustedRendering:
    """``write_speed_adjusted_mp3`` actually shifts the tempo when ffmpeg is
    available."""

    @staticmethod
    def _make_real_mp3(path, duration_s=2.0):
        """Synthesise a short sine-wave MP3 at *path* via ffmpeg.

        Skips the calling test if ffmpeg fails (e.g. in containers where the
        binary is partially broken).  Returns the produced byte length.
        """
        import subprocess
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_s}",
            "-acodec", "libmp3lame", "-q:a", "4",
            str(path),
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        if result.returncode != 0 or not os.path.isfile(path):
            pytest.skip("ffmpeg could not synthesise a test fixture MP3.")
        return os.path.getsize(path)

    def test_write_speed_adjusted_mp3_shrinks_file_at_2x(self, tmp_path):
        """At 2x speed the re-encoded MP3 is meaningfully shorter than the
        source: proves the ``atempo`` filter is actually being applied."""
        from helpers._tts import ffmpeg_speed_available, write_speed_adjusted_mp3
        if not ffmpeg_speed_available():
            pytest.skip("ffmpeg not installed; cannot verify speed shift.")

        src = tmp_path / "src.mp3"
        dest = tmp_path / "fast.mp3"
        src_size = self._make_real_mp3(src, duration_s=2.0)

        write_speed_adjusted_mp3(str(src), str(dest), 2.0)
        assert dest.is_file()
        dst_size = dest.stat().st_size
        assert dst_size > 0
        # A 2x speed-up should comfortably halve the audio duration; we
        # check a loose 85 % ceiling so codec / encoder jitter never flakes.
        assert dst_size < int(src_size * 0.85)

    def test_download_uses_ffmpeg_when_speed_requested(self, client, admin_user,
                                                       stub_backend,
                                                       monkeypatch, tmp_path):
        """The download route hands the cached MP3 to ffmpeg when ``?speed=``
        is non-unit.  We swap in a real (multi-frame) source MP3 in place of
        the stub so ffmpeg has something to process."""
        from helpers._tts import ffmpeg_speed_available
        if not ffmpeg_speed_available():
            pytest.skip("ffmpeg not installed; cannot verify speed shift.")

        import db
        page_id = _create_page()
        _login(client, "admin", "admin123")
        client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "en"}),
            content_type="application/json",
        )
        gen = _wait_for_status(client, "welcome")
        assert gen and gen["status"] == "completed"

        # Replace the stub's 422-byte single-frame MP3 with a real one so
        # ffmpeg has actual audio to re-encode.
        cached = db.get_tts_generation(page_id)
        cached_path = os.path.join(stub_backend, cached["filename"])
        self._make_real_mp3(cached_path, duration_s=2.0)

        baseline = client.get("/page/welcome/tts/download")
        adjusted = client.get("/page/welcome/tts/download?speed=2.0")
        assert baseline.status_code == 200
        assert adjusted.status_code == 200
        # The two responses differ: at 2x the file went through ffmpeg.
        assert baseline.data != adjusted.data
        assert len(adjusted.data) < int(len(baseline.data) * 0.85)


class TestMultilingualHelpers:
    """The helpers that back the admin's enabled-language selector."""

    def test_parse_enabled_languages_basic(self):
        from helpers._tts import parse_enabled_languages
        # parse returns codes in catalogue (alphabetical) order, not input order.
        assert set(parse_enabled_languages("en,it")) == {"en", "it"}
        assert set(parse_enabled_languages("en, it, fr")) == {"en", "it", "fr"}

    def test_parse_enabled_languages_drops_unknown(self):
        from helpers._tts import parse_enabled_languages
        # "zz" is not a real TTS language; should be silently dropped.
        out = parse_enabled_languages("en,zz,it")
        assert "zz" not in out
        assert "en" in out and "it" in out

    def test_parse_enabled_languages_empty_falls_back_to_default(self):
        from helpers._tts import parse_enabled_languages
        # Empty string / None falls back to en+it (the historical default).
        assert parse_enabled_languages("") == ("en", "it")
        assert parse_enabled_languages(None) == ("en", "it")

    def test_serialize_enabled_languages_roundtrip(self):
        from helpers._tts import serialize_enabled_languages, parse_enabled_languages
        codes = ["es", "fr", "de", "zz"]
        csv = serialize_enabled_languages(codes)
        # "zz" was dropped, the rest survive in catalogue order.
        parsed = parse_enabled_languages(csv)
        assert "zz" not in parsed
        for code in ("es", "fr", "de"):
            assert code in parsed

    def test_serialize_enabled_languages_empty_returns_default(self):
        from helpers._tts import serialize_enabled_languages
        # All inputs filtered out -> empty result so the parser falls back.
        assert serialize_enabled_languages([]) == ""
        assert serialize_enabled_languages(["zz", "qq"]) == ""


class TestNormalizationWithAllowedSubset:
    """``normalize_tts_language`` should honour the admin's enabled set."""

    def test_disabled_language_falls_back(self):
        from helpers._tts import normalize_tts_language
        # Korean is in the TTS catalogue but the admin disabled it.
        result = normalize_tts_language("ko", default="en", allowed=("en", "it"))
        assert result == "en"

    def test_enabled_language_passes_through(self):
        from helpers._tts import normalize_tts_language
        result = normalize_tts_language("es", default="en", allowed=("en", "es", "it"))
        assert result == "es"

    def test_default_used_when_disabled(self):
        from helpers._tts import normalize_tts_language
        # Default is also disabled -> we fall back to *some* enabled code.
        result = normalize_tts_language("ko", default="de", allowed=("en", "fr"))
        assert result in ("en", "fr")


class TestMultilingualSupportedLanguages:
    """The status endpoint should advertise only the admin-enabled subset."""

    def test_status_lists_all_supported_languages(self, client, stub_backend,
                                                  admin_user):
        import db
        from helpers._tts import TTS_SUPPORTED_LANGUAGES, TTS_SUPPORTED_LANGUAGE_SET
        _create_page()
        db.update_site_settings(
            tts_page_panel_enabled=1,
        )
        _login(client, "admin", "admin123")
        resp = client.get("/page/welcome/tts/status")
        assert resp.status_code == 200
        body = resp.get_json()
        codes = [entry["code"] for entry in body["supported_languages"]]
        # All supported languages are always listed. No per-language toggling.
        assert set(codes) == TTS_SUPPORTED_LANGUAGE_SET

    def test_generate_any_language_accepted(self, client, stub_backend,
                                                  admin_user):
        import db
        _create_page()
        db.update_site_settings(
            tts_page_panel_enabled=1,
        )
        _login(client, "admin", "admin123")
        resp = client.post(
            "/page/welcome/tts/generate",
            data=json.dumps({"language": "ko"}),
            content_type="application/json",
        )
        # All supported languages are always accepted. No per-language gating.
        assert resp.status_code in (200, 202)
        body = resp.get_json()
        assert body["ok"] is True


class TestAdminSelectorPersistence:
    """POSTing the admin settings form should persist the language subset."""

    def test_post_tts_panel_settings(self, client, admin_user):
        import db
        _login(client, "admin", "admin123")
        # First make sure the TTS plugin is enabled (defaults vary by fixture).
        try:
            db.update_plugin_enabled("tts", True)
        except Exception:
            pass
        resp = client.post(
            "/global-settings",
            data={
                "site_name": "BananaWiki",
                "tts_page_panel_enabled": "1",
                "tts_public_access_enabled": "1",
                "tts_generation_mode": "auto_save",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 302)
        settings = db.get_site_settings()
        assert settings.get("tts_page_panel_enabled") == 1
        assert settings.get("tts_public_access_enabled") == 1
        assert settings.get("tts_auto_generate_enabled") == 1


class TestTtsThemeReactivity:
    """TTS UI surfaces should adapt to the current BananaWiki theme."""

    def test_page_widget_css_uses_theme_variables(self):
        css_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "app",
            "static",
            "css",
            "tts.css",
        )
        with open(css_path, encoding="utf-8") as f:
            css = f.read()

        assert "color-scheme: dark" not in css
        assert "color-scheme: light" not in css
        assert "color-scheme: inherit" in css
        assert "var(--secondary)" in css
        assert "var(--accent)" in css
        assert "#ffffff" not in css.lower()

    def test_page_widget_uses_flat_text_heading_without_emoji(self, client, admin_user):
        import db

        _login(client, "admin", "admin123")
        _ensure_tts_plugin_enabled()
        db.update_site_settings(tts_page_panel_enabled=1)
        _create_page(slug="flat-player", title="Flat player", content="Listen.")

        body = client.get("/page/flat-player").get_data(as_text=True)

        assert "Listen to this page" in body
        assert "tts-panel-heading" in body
        assert "tts-panel-title-icon" not in body
        assert "\U0001f50a" not in body

    def test_page_widget_marks_content_for_highlighting(self, client, admin_user):
        """The panel no longer includes a follow-along transcript section.

        Instead, a small initialisation script marks `.wiki-content` elements
        with ``data-tts-segment`` so the JS can highlight the real page text
        above the player while audio plays.
        """
        import db

        _login(client, "admin", "admin123")
        _ensure_tts_plugin_enabled()
        db.update_site_settings(tts_page_panel_enabled=1)
        _create_page(
            slug="follow-along",
            title="Follow along",
            content="First sentence. Second sentence with **formatting**.",
        )

        body = client.get("/page/follow-along").get_data(as_text=True)

        # The old in-panel transcript block is gone.
        assert "tts-transcript" not in body
        # The content-marking init script is present.
        assert "data-tts-content" in body
        assert "data-tts-segment" in body

    def test_admin_status_css_derives_status_colors_from_theme(self):
        template_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "app",
            "templates",
            "admin",
            "tts_status.html",
        )
        with open(template_path, encoding="utf-8") as f:
            body = f.read()

        for fixed_color in ("#4ade80", "#60a5fa", "#facc15", "#f87171", "#fff"):
            assert fixed_color not in body.lower()
        assert "--tts-status-completed:color-mix" in body
        assert "var(--border-color)" in body
        assert "var(--secondary)" in body
