"""What the voice reads: :func:`text.speech_text`, what the synthesiser gets.

It ends every block like a sentence and leaves out what the page does not
show as text, while the hash keeps using the frozen 1.4 normalisation.
The player that follows it is tested in test_tts_player_script.py.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import service as pages
from bananawiki.wiki.features.tts import backends, service, text

# Server: the text the voice reads

RICH = """Intro with **bold** and `code` and a [link](https://example.org/a?b=c).

## Second heading

- First item
- Parent item
  - Nested child

| Name | Value |
| --- | --- |
| Alpha | One |

```python
print("not read")
```

![A banana](/static/x.png)

[[video url="https://www.youtube.com/watch?v=abc"]]

[TOC]

See https://www.example.org/some/long/path?x=1 &amp; ask @boss or mail a@b.org.
Underlined
=========
"""


def test_every_block_ends_like_a_sentence():
    assert text.speech_text("Rich page", RICH) == (
        "Rich page. Intro with bold and code and a link. Second heading. First item. Parent item. "
        "Nested child. Name, Value. Alpha, One. A banana. See example.org & ask boss or mail a@b.org. "
        "Underlined.")


def test_the_hash_still_covers_the_14_text():
    """The voice reads the new text, but audio made by 1.4 stays valid: its hash input did not change."""
    spoken = text.normalize_text("Rich page", RICH)
    assert "Second heading First item Parent item" in spoken
    assert "https://www.example.org/some/long/path?x=1" in spoken
    assert text.speech_text("Rich page", RICH) != spoken


@pytest.mark.parametrize(("title", "content", "expected"), [
    ("Questions?", "Why not", "Questions? Why not."),
    ("", "Just text", "Just text."),
    ("Only a title", "", "Only a title."),
    ("T", "<p>One</p><p>Two</p>", "T. One. Two."),
    ("T", "Inline [[canvas slug=\"x\"]] board stays one sentence.", "T. Inline board stays one sentence."),
    ("T", "Line one\nline two of the same paragraph", "T. Line one line two of the same paragraph."),
    ("T", "> quoted\n> still quoted\n\nafter", "T. quoted still quoted. after."),
    ("T", "Sentence one, then a comma,", "T. Sentence one, then a comma,"),
])
def test_speech_text_cases(title, content, expected):
    assert text.speech_text(title, content) == expected


SPEECH_PATHOLOGICAL = {
    "unclosed shortcodes": "[[video " * 50_000,
    "unclosed block tags": "<p" * 100_000 + ">",
    "addresses": "www." * 100_000,
    "mentions": "@" * 200_000 + "a",
    "quote lines": "> a\n" * 100_000,
    "table lines": "| a | b |\n" * 50_000,
    "open brackets": "[" * 200_000,
    "open tags": "<" * 200_000,
}


@pytest.mark.parametrize("content", SPEECH_PATHOLOGICAL.values(), ids=SPEECH_PATHOLOGICAL.keys())
def test_speech_text_is_linear_and_bounded(content):
    started = time.perf_counter()
    spoken = text.speech_text("Title", content)
    assert time.perf_counter() - started < 3.0
    assert len(spoken) <= text.MAX_INPUT_CHARS


def test_long_text_is_cut_at_a_space():
    spoken = text.speech_text("", "word " * 10_000)
    assert len(spoken) <= text.MAX_INPUT_CHARS and spoken.endswith(("word", "word."))
    assert not spoken.rstrip(".").endswith((" w", " wo", " wor")), "never half a word"


class Recorder:
    name = "stub"

    def __init__(self):
        self.calls: list[str] = []

    def problem(self):
        return None

    def synthesize(self, spoken, language, workdir: Path):
        self.calls.append(spoken)
        path = workdir / f".part-speech-{len(self.calls)}.mp3"
        path.write_bytes(b"ID3" + b"\x00" * 200)
        return backends.Audio(path, "mp3")


def test_the_worker_reads_the_speech_text_and_keeps_the_14_hash(app):
    from flask import g

    recorder = Recorder()
    with app.test_request_context(), connection_scope():
        g.user = g.real_user = None
        page = pages.create("Guide", "## Steps\n\n- Peel\n- Eat", category_id=None, author_id=None)
        service.request(page, requested_by=None)
        job = service.claim()
        assert service.process(job, recorder) == "completed"
        row = service.generation(page["id"])
        assert service.page_audio(pages.get(page["id"])).usable
    assert recorder.calls == ["Guide. Steps. Peel. Eat."]
    assert row["content_hash"] == text.content_hash(text.normalize_text("Guide", "## Steps\n\n- Peel\n- Eat"), "en")


def _long_page(tail: str) -> str:
    # About 26,000 characters of plain text: the 1.4 hash covers the first 20,000 of them.
    paragraphs = [f"Paragraph {n}: see https://example.org/a/very/long/path/{n}?ref=docs for more." for n in range(320)]
    return "\n\n".join(paragraphs) + "\n\n" + tail


def test_the_voice_never_reads_past_what_the_hash_covers():
    """Shortened addresses make the speech text more compact than the hashed one; it still stops where the
    hash does, so an edit beyond that point changes neither the hash nor what is read."""
    before, after = _long_page("The old ending."), _long_page("A completely different ending.")
    assert text.spoken_text("Long", before) == text.spoken_text("Long", after)
    assert text.speech_text("Long", before) == text.speech_text("Long", after)
    speech, cut = text.speech_info("Long", before)
    assert cut and "ending" not in speech and "Paragraph 0" in speech
    last = speech.rsplit("Paragraph ", 1)[1].split(":")[0]
    assert f"Paragraph {last}:" in text.spoken_text("Long", before), "a paragraph the hash covers"


def test_the_player_is_told_where_the_audio_stops():
    assert service.cut_at({"title": "Short", "content": "A short page."}) == 0
    long_page = {"title": "Long", "content": _long_page("End.")}
    assert service.cut_at(long_page) == len(text.speech_text("Long", long_page["content"])) > 0
    # Just under the limit when hashed, but more than the limit once every block ends like a sentence.
    blocks = "\n\n".join(["- item"] * 2_400)
    assert len(text.spoken_text("", blocks)) < text.MAX_INPUT_CHARS
    assert service.cut_at({"title": "", "content": blocks}) == (
        len(text.speech_text("", blocks)) if text.speech_info("", blocks)[1] else 0)
