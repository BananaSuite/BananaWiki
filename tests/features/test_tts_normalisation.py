"""Read-aloud text: 1.4-identical normalisation in linear time, kept out of the write lock."""

from __future__ import annotations

import random
import re
import time
import unicodedata
import uuid
from contextlib import contextmanager

import pytest
from flask import g

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.db import db as wiki_db
from bananawiki.wiki.features.pages import service as pages
from bananawiki.wiki.features.tts import backends, service, text

# ── The 1.4 normalisation, kept as the reference ─────────────────────────────

_LEGACY = [
    (re.compile(r"```(?:\w*\n?)?(.*?)```", re.DOTALL), " "),
    (re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.MULTILINE), " "),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"(\*{1,3}|_{1,3}|~~)(.+?)\1"), r"\2"),
    (re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?(?:[ \t]*\|[ \t]*:?-{2,}:?)+[ \t]*\|?[ \t]*$", re.MULTILINE), " "),
    (re.compile(r"^[ \t]*(?:[-*_]\s*){3,}\s*$", re.MULTILINE), " "),
    (re.compile(r"^[ \t]*(?:#+|[>\-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?", re.MULTILINE), ""),
    (re.compile(r"<[^>]+>"), " "),
    (re.compile(r"[*_~`|]+"), " "),
]


def legacy_normalize(title: str, content: str) -> str:
    body = content
    for pattern, repl in _LEGACY:
        body = pattern.sub(repl, body)
    body = re.sub(r"\s+", " ", unicodedata.normalize("NFC", body)).strip()
    title = title.strip()
    spoken = (f"{title}. {body}" if body else title) if title else body
    return spoken[:text.MAX_INPUT_CHARS]


PAGE = """# Guida alla fotosintesi

La **fotosintesi** è il processo con cui le piante producono _zuccheri_ e ~~anidride~~ ossigeno.
Vedi [Wikipedia](https://it.wikipedia.org/wiki/Fotosintesi_(biologia)) e la [guida][1].

![Schema della foglia](/uploads/foglia.png "Foglia")

## Fasi

1. Fase luminosa
2) Fase oscura (ciclo di Calvin)
- [x] Letto il capitolo
* [ ] Esercizi da fare

> Nota: la clorofilla assorbe la luce <em>rossa</em> e blu.

| Fase | Luogo |
|:-----|------:|
| Luminosa | Tilacoidi |

---

Usa `print("ciao")` per provare:

```python
print("non letto")
```

Se 3 < 5 allora<br>si va a capo.

[1]: https://example.org/guida "Guida"
"""

PAGE_SPOKEN = (
    "Fotosintesi. Guida alla fotosintesi La fotosintesi è il processo con cui le piante producono zuccheri e "
    "anidride ossigeno. Vedi Wikipedia) e la [guida][1]. Schema della foglia Fasi Fase luminosa Fase oscura "
    "(ciclo di Calvin) Letto il capitolo Esercizi da fare Nota: la clorofilla assorbe la luce rossa e blu. "
    "Fase Luogo Luminosa Tilacoidi Usa print(\"ciao\") per provare: Se 3 si va a capo."
)


def test_representative_page_keeps_its_1x_text_and_hash():
    """Existing audio stays valid: same text (quirks included) and same hash as 1.4."""
    spoken = text.normalize_text("Fotosintesi", PAGE)
    assert spoken == PAGE_SPOKEN == legacy_normalize("Fotosintesi", PAGE)
    assert text.content_hash(spoken, "it") == text.content_hash(legacy_normalize("Fotosintesi", PAGE), "it")
    crlf = PAGE.replace("\n", "\r\n")
    assert text.normalize_text("Fotosintesi", crlf) == legacy_normalize("Fotosintesi", crlf)


_TOKENS = [
    "[", "]", "(", ")", "![", "](", "]: ", "<", ">", "<b>", "</b>", "`", "```", "```py\n", "**", "*", "_", "__",
    "~~", "|", "|---|---|", ":--", "---", "***", "# ", "## ", "> ", "- ", "* ", "+ ", "1. ", "2) ", "- [ ] ",
    "- [x] ", "\n", "\n\n", "\r\n", " ", "\t", "  ", "\xa0", "\x0c", "word", "x", "http://e.org/a_(b)", "e\u0301",
    "3 < 5", "a > b",
]


def test_normalisation_matches_1x_on_random_markdown():
    rng = random.Random(20261004)
    for _ in range(4000):
        content = "".join(rng.choice(_TOKENS) for _ in range(rng.randint(0, 40)))
        assert text.normalize_text("T", content) == legacy_normalize("T", content), repr(content)


PATHOLOGICAL = {
    "open brackets": "[" * 200_000,
    "brackets closed far away": "[" * 200_000 + "]x)",
    "unterminated links": "[a](" * 50_000,
    "open images": "![" * 100_000,
    "unterminated images": "![a](" * 40_000,
    "open tags": "<" * 200_000,
    "blank lines": "\n" * 200_000,
    "bracket lines": "[\n" * 100_000,
    "unclosed fence": "```" + "a" * 200_000,
    "leading spaces": " " * 200_000 + "x",
    "rule then spaces": "---" + " " * 200_000 + "x",
    "table then spaces": "|--|--" + " " * 200_000 + "x",
}


@pytest.mark.parametrize("content", PATHOLOGICAL.values(), ids=PATHOLOGICAL.keys())
def test_pathological_text_normalises_in_linear_time(content):
    # 1.4 spent tens of seconds to minutes on each of these; linear work takes milliseconds.
    started = time.perf_counter()
    text.normalize_text("Title", content)
    assert time.perf_counter() - started < 3.0


def test_text_beyond_the_page_limit_is_ignored(monkeypatch):
    monkeypatch.setattr(text, "MAX_SOURCE_CHARS", 10)
    assert text.normalize_text("", "0123456789 [never read](x)") == "0123456789"


def test_spoken_text_is_remembered_per_content(monkeypatch):
    calls = []
    original = text.normalize_text
    monkeypatch.setattr(text, "normalize_text", lambda *args: calls.append(args) or original(*args))
    content = f"Words about {uuid.uuid4().hex}."
    assert text.spoken_text("A", content) == text.spoken_text("A", content) == original("A", content)
    assert len(calls) == 1
    text.spoken_text("B", content)
    text.spoken_text("A", content + " More.")
    assert len(calls) == 3
    for index in range(text.SPOKEN_CACHE_SIZE + 5):
        text.spoken_text("Filler", f"{content} {index}")
    assert len(text._spoken_cache) == text.SPOKEN_CACHE_SIZE


# ── Where the text is normalised ──────────────────────────────────────────────


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
    def create(title="Guide", content=None):
        content = content or f"This page about bananas has enough words to be read, {uuid.uuid4().hex}."
        with ctx():
            return pages.create(title, content, author_id=None)
    return create


class Synth:
    name = "stub"

    def problem(self):
        return None

    def synthesize(self, spoken, language, workdir):
        path = workdir / f".part-{uuid.uuid4().hex}.mp3"
        path.write_bytes(b"ID3" + b"\x00" * 200)
        return backends.Audio(path, "mp3")


@pytest.fixture
def synth(app):
    fake = Synth()
    app.extensions["bananawiki.tts.synthesizer"] = fake
    return fake


@pytest.fixture
def normalisations(monkeypatch):
    """Every call to normalize_text, with whether a write transaction was open."""
    calls: list[bool] = []
    original = text.normalize_text

    def spy(title, content):
        calls.append(wiki_db.conn.in_transaction)
        return original(title, content)

    monkeypatch.setattr(text, "normalize_text", spy)
    text._spoken_cache.clear()
    return calls


def test_claim_normalises_outside_the_write_transaction(ctx, make_page, normalisations, db):
    page = make_page()
    with ctx():
        service.request(page, requested_by=None)
        text._spoken_cache.clear()
        job = service.claim()
    assert job is not None and job.spoken.startswith("Guide. This page about bananas")
    assert normalisations == [False, False]
    assert db.scalar("SELECT status FROM tts_generations WHERE id = ?", (job.id,)) == "processing"


def test_claim_skips_a_page_edited_while_it_was_read(ctx, make_page, monkeypatch, db):
    page = make_page()
    with ctx():
        service.request(page, requested_by=None)
    original = text.spoken_text

    def edit_meanwhile(title, content):
        db.execute("UPDATE pages SET content = 'Edited while the worker read it, with other words.' WHERE id = ?",
                   (page["id"],))
        monkeypatch.setattr(text, "spoken_text", original)
        return original(title, content)

    monkeypatch.setattr(text, "spoken_text", edit_meanwhile)
    with ctx():
        assert service.claim(scan=1) is None
        assert db.scalar("SELECT status FROM tts_generations WHERE page_id = ?", (page["id"],)) == "pending"
        job = service.claim()
    assert job is not None and "Edited while the worker read it" in job.spoken


def _completed(app, ctx, page, synth):
    with ctx():
        service.request(page, requested_by=None)
    with app.test_request_context(), connection_scope():
        assert service.process(service.claim(), synth) == "completed"


def test_status_and_page_views_do_not_normalise_again(app, client, make_user, login, make_page, ctx, synth,
                                                      normalisations):
    page = make_page()
    _completed(app, ctx, page, synth)
    login(client, make_user("reader"))
    text._spoken_cache.clear()
    normalisations.clear()
    assert client.get("/page/guide/tts/status").get_json()["usable"] is True
    assert len(normalisations) == 1
    for _ in range(3):
        assert client.get("/page/guide/tts/status").get_json()["usable"] is True
        assert client.get("/page/guide").status_code == 200
        assert client.get("/page/guide/tts/audio").status_code == 200
    assert len(normalisations) == 1


def test_refused_generate_does_not_normalise(app, client, make_user, login, make_page, normalisations, db):
    make_page("One")
    make_page("Two")
    login(client, make_user("reader"))
    assert client.post("/page/one/tts/generate", json={}).status_code == 202
    normalisations.clear()
    assert client.post("/page/one/tts/generate", json={}).status_code == 409  # already in progress
    response = client.post("/page/two/tts/generate", json={})
    assert response.status_code == 503 and response.get_json()["error"] == "queue_full"
    response = client.post("/page/two/tts/generate", json={"language": "ko"})
    assert response.status_code == 400 and response.get_json()["error"] == "unsupported_language"
    assert normalisations == []


def test_generate_on_current_audio_reports_it(app, client, make_user, login, make_page, ctx, synth):
    page = make_page()
    _completed(app, ctx, page, synth)
    login(client, make_user("reader"))
    response = client.post("/page/guide/tts/generate", json={})
    body = response.get_json()
    assert response.status_code == 409 and body["error"] == "already_generated"
    assert body["generation"]["status"] == "completed" and body["generation"]["has_file"] is True
