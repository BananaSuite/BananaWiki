"""What readers and administrators are told about read-aloud jobs.

The status endpoint says how many jobs come first, whether any worker is
running (every worker touches a heartbeat file in the audio folder) and,
for a long page, where its audio stops. The admin page warns when pages are
queued and no worker has checked in, and says when one last did.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from flask import g

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import service as pages
from bananawiki.wiki.features.tts import backends, service, text, worker


class Synth:
    name = "stub"

    def problem(self):
        return None

    def synthesize(self, spoken, language, workdir: Path):
        path = workdir / ".part-state.mp3"
        path.write_bytes(b"ID3" + b"\x00" * 200)
        return backends.Audio(path, "mp3")


@pytest.fixture
def ctx(app):
    app.extensions["bananawiki.tts.synthesizer"] = Synth()

    @contextmanager
    def scope():
        with app.test_request_context(), connection_scope():
            g.user = g.real_user = None
            yield
    return scope


@pytest.fixture
def make_page(ctx):
    def create(title, content="Some words about bananas, enough of them to be read aloud."):
        with ctx():
            return pages.create(title, content, category_id=None, author_id=None)
    return create


def _heartbeat(app) -> Path:
    with app.app_context():
        return service.folder() / service.HEARTBEAT_FILE


def test_queued_pages_learn_their_place_and_whether_a_worker_runs(app, client, make_user, login, make_page, ctx):
    first, second = make_page("One"), make_page("Two")
    with ctx():
        service.request(first, requested_by=None)
        service.request(second, requested_by=None)
    login(client, make_user("reader"))
    state = client.get("/page/two/tts/status").get_json()
    assert state["in_flight"] and state["queue_ahead"] == 1 and state["worker_running"] is False
    assert client.get("/page/one/tts/status").get_json()["queue_ahead"] == 0
    with ctx():
        service.touch_heartbeat()
        assert service.claim().page_id == first["id"]
    state = client.get("/page/two/tts/status").get_json()
    assert state["worker_running"] is True and state["queue_ahead"] == 1, "a running job still comes first"
    old = time.time() - service.WORKER_STALE_SECONDS - 60
    os.utime(_heartbeat(app), (old, old))
    assert client.get("/page/two/tts/status").get_json()["worker_running"] is False


def test_every_worker_checks_in_when_it_starts(app, ctx):
    assert not _heartbeat(app).exists()
    runner = worker.Worker(app, threads=1, once=True)
    runner.start()
    runner.shutdown(5)
    assert _heartbeat(app).exists()
    with app.app_context():
        assert service.worker_running()


def test_the_sweep_and_clearing_keep_the_heartbeat(app, ctx):
    with ctx():
        service.touch_heartbeat()
    old = time.time() - 7200
    os.utime(_heartbeat(app), (old, old))
    with ctx():
        service.sweep()
        service.clear_all()
    assert _heartbeat(app).exists()


def test_a_long_page_says_where_its_audio_stops(app, client, make_user, login, make_page, ctx):
    long_text = " ".join(f"Sentence number {n} about bananas." for n in range(1500))
    long_page, short_page = make_page("Long", long_text), make_page("Short")
    for page in (long_page, short_page):
        with ctx():
            service.request(page, requested_by=None)
            assert service.process(service.claim(), Synth()) == "completed"
    login(client, make_user("reader"))
    cut = client.get("/page/long/tts/status").get_json()["cut_at"]
    with ctx():
        assert cut == len(text.speech_text("Long", long_text)) and 0 < cut <= text.MAX_INPUT_CHARS
    assert client.get("/page/short/tts/status").get_json()["cut_at"] == 0
    html = client.get("/page/long").get_data(as_text=True)
    assert '"cut_at": ' in html or '"cut_at":' in html


def test_the_admin_page_warns_when_no_worker_reads_the_queue(app, client, make_user, login, make_page, ctx):
    page = make_page("Queued")
    with ctx():
        service.request(page, requested_by=None)
    login(client, make_user("boss", role="admin"))
    html = client.get("/admin/tts").get_data(as_text=True)
    assert "No read-aloud worker has run yet" in html
    assert "Last check-in</th><td>Never</td>" in html
    with ctx():
        service.touch_heartbeat()
    html = client.get("/admin/tts").get_data(as_text=True)
    assert "No read-aloud worker has run yet" not in html and "last checked in" not in html
    old = time.time() - 3 * 3600
    os.utime(_heartbeat(app), (old, old))
    html = client.get("/admin/tts").get_data(as_text=True)
    assert "The read-aloud worker last checked in 3 h ago, so queued pages are waiting." in html


def test_the_admin_page_says_when_a_running_job_started(app, client, make_user, login, make_page, ctx):
    page = make_page("Running")
    with ctx():
        service.request(page, requested_by=None)
        service.touch_heartbeat()
        service.claim()
    login(client, make_user("boss", role="admin"))
    html = client.get("/admin/tts?status=processing").get_data(as_text=True)
    assert "started " in html and "Running" in html


def test_the_panel_is_named_without_ids_a_heading_could_take(app, client, make_user, login, make_page):
    make_page("Guide", "## TTS title 1\n\n## TTS speed 1\n\nSome words about bananas, enough of them to read.")
    login(client, make_user("reader"))
    html = client.get("/page/guide").get_data(as_text=True)
    panel = html[html.index("data-tts-panel"):]
    assert 'aria-labelledby="tts-title' not in panel and 'for="tts-speed' not in panel
    assert 'aria-label="Listen to this page"' in html
    assert '<label class="tts-panel__speed small">Speed' in panel, "the label holds its select"
