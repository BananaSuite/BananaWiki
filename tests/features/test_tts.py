"""Read aloud: queue, invalidation, permissions, limits and the admin page."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from flask import g

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import categories
from bananawiki.wiki.features.pages import service as pages
from bananawiki.wiki.features.tts import backends, options, service, text, worker

AUDIO = b"ID3" + b"\x00" * 200


class FakeSynth:
    """Writes a small MP3 or raises the configured error."""

    name = "stub"

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.error: backends.SynthesisError | None = None
        self.unavailable: str | None = None

    def problem(self):
        return self.unavailable

    def synthesize(self, spoken, language, workdir: Path):
        self.calls.append((spoken, language))
        if self.error:
            raise self.error
        path = workdir / f".part-test-{len(self.calls)}.mp3"
        path.write_bytes(AUDIO)
        return backends.Audio(path, "mp3")


@pytest.fixture
def synth(app):
    fake = FakeSynth()
    app.extensions["bananawiki.tts.synthesizer"] = fake
    return fake


@pytest.fixture
def ctx(app):
    @contextmanager
    def scope(user=None):
        with app.test_request_context(), connection_scope():
            g.user = g.real_user = user
            yield
    return scope


@pytest.fixture
def make_page(ctx):
    def create(title="Guide", content="This is a page about bananas and it has enough words to be read.",
               category_id=None):
        with ctx():
            return pages.create(title, content, category_id=category_id, author_id=None)
    return create


@pytest.fixture
def work(app, synth):
    def run():
        with app.test_request_context(), connection_scope():
            return worker.run_once()
    return run


def set_config(app, **values):
    cfg = options.load(app.config["BW"], {})
    app.extensions["bananawiki.tts.config"] = options.TtsConfig(**{**cfg.__dict__, **values})


def generation(db, page):
    return db.one("SELECT * FROM tts_generations WHERE page_id = ?", (page["id"],))


def tts_files(app):
    folder = Path(app.config["BW"].folders.tts)
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# ── Text ──────────────────────────────────────────────────────────────────────


def test_normalisation_and_hash_match_1x():
    content = "# Intro\n\nRead **this** [link](http://x).\n\n```\ncode\n```"
    spoken = text.normalize_text("Guide", content)
    assert spoken == "Guide. Intro Read this link."
    assert text.content_hash(spoken, "en") == "846618076a81c00b63ccc76482275e96c75af2177794ff1da5f610b8b00d1b4a"


def test_language_detection_stays_in_enabled_set():
    italian = "Questa è una pagina della wiki con molte parole che sono scritte in italiano per il test"
    assert text.detect_language(italian, allowed=("en", "it")) == "it"
    assert text.detect_language(italian, allowed=("en", "de")) == "en"
    assert text.parse_enabled_languages("xx,de,en") == ("de", "en")
    assert text.parse_enabled_languages("") == ("en", "it")
    assert text.normalize_language("Italian", allowed=("it",)) == "it"
    assert text.normalize_language("fr", allowed=("en",)) is None
    assert text.normalize_speed("1,25") == 1.25 and text.normalize_speed("9") == 1.0


# ── Main flow ─────────────────────────────────────────────────────────────────


def test_generate_work_play_and_download(client, make_user, login, make_page, work, db, app, synth):
    page = make_page()
    login(client, make_user("reader"))
    assert client.get("/page/guide/tts/status").get_json()["can_generate"] is True
    response = client.post("/page/guide/tts/generate", json={"language": "auto"})
    assert response.status_code == 202
    assert generation(db, page)["status"] == "pending"
    assert client.post("/page/guide/tts/generate", json={}).status_code == 409

    assert work() == "completed"
    row = generation(db, page)
    assert row["status"] == "completed" and row["file_size"] == len(AUDIO) and row["language"] == "en"
    assert row["filename"] == f"tts_{page['id']}_en_{row['content_hash'][:8]}_{row['id']}.mp3"
    assert synth.calls[0][0].startswith("Guide. This is a page")

    state = client.get("/page/guide/tts/status").get_json()
    assert state["usable"] and not state["can_generate"]
    audio = client.get("/page/guide/tts/audio")
    assert audio.status_code == 200 and audio.mimetype == "audio/mpeg" and audio.data == AUDIO
    download = client.get("/page/guide/tts/download")
    assert "attachment" in download.headers["Content-Disposition"]
    assert "guide-en.mp3" in download.headers["Content-Disposition"]
    assert client.post("/page/guide/tts/generate", json={}).status_code == 409


def test_claims_are_exclusive(app, make_page, ctx):
    first, second = make_page("One"), make_page("Two")
    with ctx():
        service.request(first, requested_by=None)
        service.request(second, requested_by=None)
        jobs = [service.claim(), service.claim(), service.claim()]
    assert {jobs[0].page_id, jobs[1].page_id} == {first["id"], second["id"]} and jobs[2] is None


def test_manual_limits(app, client, make_user, login, make_page, db):
    make_page("One")
    make_page("Two")
    make_page("Three")
    login(client, make_user("reader"))
    assert client.post("/page/one/tts/generate", json={}).status_code == 202
    response = client.post("/page/two/tts/generate", json={})
    assert response.status_code == 503 and response.get_json()["error"] == "queue_full"
    set_config(app, manual_max_active_jobs=5, manual_max_active_per_user=1)
    response = client.post("/page/three/tts/generate", json={})
    assert response.status_code == 429 and response.get_json()["error"] == "user_queue_full"
    assert db.scalar("SELECT COUNT(*) FROM tts_generations") == 1


def test_host_disabled_and_panel_off(app, client, make_user, login, make_page, db):
    make_page()
    login(client, make_user("reader"))
    set_config(app, host_disabled=True)
    assert client.post("/page/guide/tts/generate", json={}).status_code == 503
    set_config(app, host_disabled=False)
    db.execute("UPDATE site_settings SET tts_page_panel_enabled = 0")
    assert client.post("/page/guide/tts/generate", json={}).status_code == 403


def test_unsupported_language_is_refused(client, make_user, login, make_page, db):
    make_page()
    login(client, make_user("reader"))
    response = client.post("/page/guide/tts/generate", json={"language": "ko"})
    assert response.status_code == 400 and response.get_json()["error"] == "unsupported_language"
    assert client.post("/page/guide/tts/generate", json={"language": "it"}).status_code == 202
    assert db.scalar("SELECT language FROM tts_generations") == "it"


# ── Invalidation and cleanup ──────────────────────────────────────────────────


def _complete(ctx, page, synth_app_work):
    with ctx():
        service.request(page, requested_by=None)
    assert synth_app_work() == "completed"


def test_edit_replaces_audio_and_deletes_old_file(app, ctx, make_page, work, db):
    page = make_page()
    _complete(ctx, page, work)
    old = generation(db, page)
    assert old["filename"] in tts_files(app)
    with ctx():
        pages.update(pages.get(page["id"]), author_id=None, content="Completely different words about apples now.")
    new = generation(db, page)
    assert new["status"] == "pending" and new["id"] != old["id"] and new["content_hash"] != old["content_hash"]
    assert old["filename"] not in tts_files(app)


def test_edit_without_audio_does_not_queue_unless_auto(app, ctx, make_page, db):
    page = make_page()
    with ctx():
        pages.update(pages.get(page["id"]), author_id=None, content="Something new to say here today.")
    assert generation(db, page) is None
    db.execute("UPDATE site_settings SET tts_auto_generate_enabled = 1")
    created = make_page("Auto page")
    assert generation(db, created)["status"] == "pending"


def test_delete_removes_files_and_variants(app, ctx, make_page, work, db):
    page = make_page()
    _complete(ctx, page, work)
    row = generation(db, page)
    folder = Path(app.config["BW"].folders.tts)
    (folder / (row["filename"].rsplit(".", 1)[0] + ".1.5x.mp3")).write_bytes(AUDIO)
    with ctx():
        pages.delete(pages.get(page["id"]), actor_id=None)
    assert generation(db, page) is None
    assert not [name for name in tts_files(app) if name.startswith(f"tts_{page['id']}_")]


def test_sweep_removes_orphans_and_refreshes_stale_audio(app, ctx, make_page, work, db):
    page = make_page()
    _complete(ctx, page, work)
    folder = Path(app.config["BW"].folders.tts)
    orphan = folder / "tts_999_en_deadbeef_1.mp3"
    orphan.write_bytes(AUDIO)
    fresh_orphan = folder / "tts_998_en_deadbeef_2.mp3"
    fresh_orphan.write_bytes(AUDIO)
    old = time.time() - 2 * service.ORPHAN_MIN_AGE_SECONDS
    os.utime(orphan, (old, old))
    db.execute("UPDATE pages SET content = 'Changed behind the feature''s back entirely.' WHERE id = ?",
               (page["id"],))
    with ctx():
        result = service.sweep()
    assert result == {"orphans": 1, "refreshed": 1}
    assert not orphan.exists() and fresh_orphan.exists()
    assert generation(db, page)["status"] == "pending"


def test_stale_audio_is_not_served(app, client, admin_client, ctx, make_page, work, db):
    page = make_page()
    _complete(ctx, page, work)
    db.execute("UPDATE pages SET content = 'Edited while read aloud was switched off.' WHERE id = ?", (page["id"],))
    assert client.get("/page/guide/tts/audio").status_code == 404
    assert client.get("/page/guide/tts/status").get_json()["can_generate"] is True


# ── Retries ───────────────────────────────────────────────────────────────────


def test_retryable_failure_backs_off_then_fails(app, ctx, make_page, work, synth, db):
    set_config(app, max_auto_resume_attempts=1, resume_base_delay=60)
    page = make_page()
    with ctx():
        service.request(page, requested_by=None)
    synth.error = backends.SynthesisError("GPU unreachable")
    assert work() == "retry"
    row = generation(db, page)
    assert row["status"] == "pending" and row["retry_count"] == 1 and row["not_before"]
    assert work() is None  # still waiting
    db.execute("UPDATE tts_generations SET not_before = NULL")
    assert work() == "failed"
    assert generation(db, page)["error_message"] == "GPU unreachable"


def test_rate_limit_does_not_use_retry_budget(app, ctx, make_page, work, synth, db):
    page = make_page()
    with ctx():
        service.request(page, requested_by=None)
    synth.error = backends.SynthesisError("HTTP 429", rate_limited=True)
    assert work() == "cooldown"
    row = generation(db, page)
    assert row["status"] == "pending" and row["retry_count"] == 0 and row["not_before"]


def test_permanent_failure_is_not_retried(app, ctx, make_page, work, synth, db):
    page = make_page()
    with ctx():
        service.request(page, requested_by=None)
    synth.error = backends.SynthesisError("No voice", retryable=False)
    assert work() == "failed"
    assert generation(db, page)["retry_count"] == 0


def test_stale_lease_and_release_requeue(app, ctx, make_page, db):
    page = make_page()
    with ctx():
        service.request(page, requested_by=None)
        job = service.claim()
        service.release(job.id)
        assert generation(db, page)["status"] == "pending"
        job = service.claim()
        db.execute("UPDATE tts_generations SET lease_until = '2000-01-01 00:00:00'")
        assert service.recover_stale() == 1
    assert generation(db, page)["status"] == "pending"


def test_superseded_job_discards_its_file(app, ctx, make_page, synth, db):
    page = make_page()
    with ctx():
        service.request(page, requested_by=None)
        job = service.claim()
        db.execute("DELETE FROM tts_generations")
        assert service.process(job, synth) == "cancelled"
    assert tts_files(app) == []


def test_worker_idles_when_blocked(app, ctx, make_page, work, synth, db):
    page = make_page()
    with ctx():
        service.request(page, requested_by=None)
    synth.unavailable = "Piper is not installed."
    assert work() is None
    synth.unavailable = None
    set_config(app, host_disabled=True)
    assert work() is None
    set_config(app, host_disabled=False)
    with ctx():
        from bananawiki.wiki.registry import set_enabled

        set_enabled("tts", False)
    assert work() is None
    assert generation(db, page)["status"] == "pending"


def test_worker_threads_process_and_shut_down(app, ctx, make_page, synth, db):
    page = make_page()
    with ctx():
        service.request(page, requested_by=None)
    runner = worker.Worker(app, threads=2, poll_interval=0.1, once=True)
    runner.start()
    deadline = time.time() + 10
    while runner.alive() and time.time() < deadline:
        time.sleep(0.05)
    runner.shutdown(1)
    assert generation(db, page)["status"] == "completed"


def test_default_backend_is_local_piper(app, ctx):
    with ctx():
        backend = backends.current()
        assert backend.name == "piper"
        assert (backend.problem() is None) == backends.piper_installed()


# ── Permissions ───────────────────────────────────────────────────────────────


def test_restricted_page_is_invisible(app, client, make_user, login, make_page, ctx, work, db):
    with ctx():
        secret = categories.create("Secret")
    page = make_page("Hidden plan", category_id=secret["id"])
    _complete(ctx, page, work)
    reader = make_user("reader")
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 1)",
               (reader["id"],))
    login(client, reader)
    for suffix in ("status", "audio", "download"):
        assert client.get(f"/page/hidden-plan/tts/{suffix}").status_code == 404
    assert client.post("/page/hidden-plan/tts/generate", json={}).status_code == 404
    assert client.post("/page/hidden-plan/tts/cancel", json={}).status_code == 404


def test_cancel_requires_edit_rights(app, client, make_user, login, make_page, ctx, work, db):
    page = make_page()
    _complete(ctx, page, work)
    login(client, make_user("reader"))
    assert client.post("/page/guide/tts/cancel", json={}).status_code == 403
    client.post("/logout")
    login(client, make_user("boss", role="admin"))
    assert client.post("/page/guide/tts/cancel", json={}).status_code == 200
    assert generation(db, page) is None and tts_files(app) == []


def test_anonymous_access_follows_public_mode(app, client, make_page, ctx, work, db):
    page = make_page()
    _complete(ctx, page, work)
    assert client.get("/page/guide/tts/audio").status_code == 302  # private wiki: sign in
    db.execute("UPDATE site_settings SET public_mode = 1")
    assert client.get("/page/guide/tts/audio").status_code == 200
    assert client.get("/page/guide/tts/status").get_json()["can_generate"] is False
    assert client.post("/page/guide/tts/generate", json={}).status_code in (302, 401)
    db.execute("UPDATE pages SET is_deindexed = 1 WHERE id = ?", (page["id"],))
    assert client.get("/page/guide/tts/audio").status_code == 404
    db.execute("UPDATE pages SET is_deindexed = 0 WHERE id = ?", (page["id"],))
    db.execute("UPDATE site_settings SET tts_public_access_enabled = 0")
    assert client.get("/page/guide/tts/audio").status_code == 404


def test_feature_switched_off_hides_endpoints(app, client, make_user, login, make_page, ctx):
    make_page()
    login(client, make_user("reader"))
    with ctx():
        from bananawiki.wiki.registry import set_enabled

        set_enabled("tts", False)
    assert client.get("/page/guide/tts/status").status_code == 404


def test_panel_slot(app, client, make_user, login, make_page, ctx, db):
    page = make_page()
    login(client, make_user("viewer"))
    view = client.get("/page/guide")
    assert view.status_code == 200 and b"data-tts-panel" in view.data and b"/static/tts/tts.js" in view.data
    assert client.get("/static/tts/tts.js").status_code == 200
    with ctx(make_user("reader")):
        from bananawiki.wiki.features.tts.routes import render_panel

        html = str(render_panel(page=pages.get(page["id"])))
        assert "data-tts-panel" in html and "/page/guide/tts/status" in html and "<script src=" in html
        db.execute("UPDATE site_settings SET tts_page_panel_enabled = 0")
        g.pop("_site_settings", None)
        assert render_panel(page=page) == ""
    with ctx(None):
        db.execute("UPDATE site_settings SET tts_page_panel_enabled = 1, public_mode = 1")
        assert render_panel(page=page) == ""  # visitors see the player only when audio exists


# ── Speed downloads ───────────────────────────────────────────────────────────


def test_speed_download_uses_cached_variant(app, client, make_user, login, make_page, ctx, work, monkeypatch):
    page = make_page()
    _complete(ctx, page, work)
    calls = []

    def fake_encode(source, target, *, speed=1.0):
        calls.append(speed)
        Path(target).write_bytes(AUDIO + b"fast")

    monkeypatch.setattr(backends, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(backends, "encode_mp3", fake_encode)
    login(client, make_user("reader"))
    for _ in range(2):
        response = client.get("/page/guide/tts/download?speed=1.5")
        assert response.status_code == 200 and response.data.endswith(b"fast")
        assert "guide-en-1.5x.mp3" in response.headers["Content-Disposition"]
    assert calls == [1.5]
    assert client.get("/page/guide/tts/download?speed=7").data == AUDIO


# ── Admin ─────────────────────────────────────────────────────────────────────


def test_admin_page_and_actions(app, client, admin_client, make_user, make_page, ctx, work, synth, db):
    page = make_page()
    make_page("Second", "More words to read in the second page of this wiki.")
    response = admin_client.get("/admin/tts")
    assert response.status_code == 200 and b"Read aloud" in response.data
    assert admin_client.get("/admin/tts-status").status_code == 301
    admin_client.post("/admin/tts-status/backfill")
    assert db.scalar("SELECT COUNT(*) FROM tts_generations WHERE status = 'pending'") >= 2
    synth.error = backends.SynthesisError("broken", retryable=False)
    assert work() == "failed"
    failed = db.one("SELECT * FROM tts_generations WHERE status = 'failed'")
    admin_client.post(f"/admin/tts/{failed['id']}/retry")
    assert db.one("SELECT * FROM tts_generations WHERE page_id = ?", (failed["page_id"],))["status"] == "pending"
    assert admin_client.get("/admin/tts?status=pending").status_code == 200
    row = generation(db, page)
    admin_client.post(f"/admin/tts/{row['id']}/delete")
    assert generation(db, page) is None
    admin_client.post("/admin/tts/clear")
    assert db.scalar("SELECT COUNT(*) FROM tts_generations") == 0


def test_admin_settings_encrypt_token_and_validate_url(admin_client, db):
    response = admin_client.post("/admin/tts/settings", data={
        "tts_page_panel_enabled": "1", "generation_mode": "auto", "languages": ["de", "en", "zz"],
        "tts_performance_mode": "fast", "tts_gpu_enabled": "1", "tts_gpu_url": "http://100.64.0.2:8787",
        "tts_gpu_auth_token": "s3cret-token", "tts_gpu_timeout": "90",
    })
    assert response.status_code == 302
    row = db.one("SELECT * FROM site_settings WHERE id = 1")
    assert row["tts_auto_generate_enabled"] == 1 and row["tts_public_access_enabled"] == 0
    assert row["tts_enabled_languages"] == "de,en" and row["tts_performance_mode"] == "fast"
    assert row["tts_gpu_auth_token"].startswith("fernet:") and row["tts_gpu_timeout"] == 90
    admin_client.post("/admin/tts/settings", data={"tts_gpu_url": "http://host/x?y=1"})
    assert db.scalar("SELECT tts_gpu_url FROM site_settings") == "http://100.64.0.2:8787"
    admin_client.post("/admin/tts/settings", data={"tts_gpu_url": "http://100.64.0.2:8787"})
    assert db.scalar("SELECT tts_gpu_auth_token FROM site_settings").startswith("fernet:")  # kept


def test_admin_requires_admin(client, make_user, login):
    login(client, make_user("editor", role="editor"))
    assert client.get("/admin/tts").status_code == 403
    assert client.post("/admin/tts/clear").status_code == 403
    assert client.post("/admin/tts/settings", data={}).status_code == 403
