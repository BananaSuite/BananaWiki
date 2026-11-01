"""Regression tests for the QW2 findings of the full re-audit pass 11 (search without FTS5, PDF export cost)."""

from __future__ import annotations

import time

from bananawiki.wiki.db import connection_scope

EMOJI = "\U0001F601"


# QW2-1: /search without FTS5 matches a term's first LIKE_CHARS characters and stops at the deadline


def _planted(app, count: int, size: int) -> None:
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        for n in range(count):
            service.create(f"Planted {n}", EMOJI * size, author_id=None)
        service.create("Party", "x" * 40 + " cake", author_id=None)


def test_qw2_1_search_like_fallback_is_short_and_stops_at_the_deadline(app, db, monkeypatch):
    from bananawiki.wiki.features.pages import service

    _planted(app, 12, 400_000)
    app.extensions["bananawiki.fts"] = False  # a SQLite without FTS5 matches every term with LIKE
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    client = app.test_client()
    monkeypatch.setattr(service, "SEARCH_SECONDS", 0.2)
    statuses = []
    # Each planted page almost matches at every position: the cost is the matched length times the page's.
    near_miss = EMOJI * 31 + "a" * 120
    for query in (EMOJI * 150 + "a", "content:" + EMOJI * 150 + "a", near_miss, "content:" + near_miss):
        started = time.monotonic()
        response = client.get("/search", query_string={"q": query})
        elapsed = time.monotonic() - started
        assert response.status_code in (200, 503) and elapsed < 0.8, (query[:12], elapsed)
        statuses.append(response.status_code)
    assert 503 in statuses
    # Only the first LIKE_CHARS characters of a term are matched.
    monkeypatch.setattr(service, "SEARCH_SECONDS", 5.0)
    found = client.get("/search", query_string={"q": "x" * 32 + "notinthepage", "type": "page"})
    assert found.status_code == 200 and b"Party" in found.data


# QW2-2: a PDF's layout is linear in the page, cut after MAX_PAGES pages, and made once whatever the Host header


def _paragraphs(count: int) -> str:
    return "\n\n".join(f"{n} " + " ".join(["lorem"] * 12) for n in range(count))


def test_qw2_2_layout_does_not_revisit_earlier_paragraphs(app, monkeypatch):
    from fpdf.text_region import Paragraph

    from bananawiki.wiki.features.page_export import pdf

    calls = 0
    build_lines = Paragraph.build_lines

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return build_lines(self, *args, **kwargs)

    monkeypatch.setattr(Paragraph, "build_lines", counted)
    with app.test_request_context():
        data = pdf.render(title="Long", content=_paragraphs(400), site_name="Wiki", base_url="https://wiki.example/",
                          meta=[])
    assert data.startswith(b"%PDF")
    assert calls < 2_000, calls  # fpdf2 alone rebuilt every earlier paragraph at each one: 80,000 calls


def test_qw2_2_a_long_pdf_is_cut_with_a_note(app, monkeypatch):
    import re

    from bananawiki.wiki.features.page_export import pdf

    notes = []
    write_note = pdf._write_cut_note
    monkeypatch.setattr(pdf, "_write_cut_note", lambda document, note: (notes.append(note), write_note(document, note)))
    monkeypatch.setattr(pdf, "MAX_PAGES", 3)
    with app.test_request_context():
        data = pdf.render(title="Long", content=_paragraphs(600), site_name="Wiki", base_url="https://wiki.example/",
                          meta=[], cut_note="Cut here")
    assert notes == ["Cut here"]
    assert len(re.findall(rb"/Type /Page\b(?!s)", data)) <= 4  # the pages laid out, and the note's at most
    # The plain-text fallback keeps the same budget, and the deadline cuts the layout too.
    notes.clear()
    monkeypatch.setattr(pdf, "MAX_PAGES", 10_000)
    monkeypatch.setattr(pdf, "LAYOUT_SECONDS", 0.0)

    def broken(*args, **kwargs):
        raise ValueError("unsupported nesting")

    monkeypatch.setattr(pdf, "_write_body", broken)
    with app.test_request_context():
        pdf.render(title="Plain", content=_paragraphs(600), site_name="Wiki", base_url="https://wiki.example/",
                   meta=[], cut_note="Cut here")
    assert notes == ["Cut here"]


def test_qw2_2_host_header_does_not_defeat_the_pdf_cache(app, admin_client, monkeypatch):
    import dataclasses

    from bananawiki.wiki.features.page_export import pdf
    from tests.features.pages_support import make_page

    make_page(app, "Hosted", "See [the other page](/page/other).")
    monkeypatch.setitem(app.config, "BW", dataclasses.replace(app.config["BW"], base_url="https://wiki.example.org"))
    layouts = []
    render = pdf._render
    monkeypatch.setattr(pdf, "_render", lambda **kwargs: (layouts.append(kwargs["body"]), render(**kwargs))[1])
    session = admin_client.get_cookie("bw_session").value
    for host in ("a.local", "b.local", "c.local"):
        admin_client.set_cookie("bw_session", session, domain=host)
        response = admin_client.get("/page/hosted/export-pdf", base_url=f"http://{host}/")
        assert response.status_code == 200 and response.data.startswith(b"%PDF")
    assert len(layouts) == 1
    assert 'href="https://wiki.example.org/page/other"' in layouts[0]


def test_qw2_2_one_long_block_is_cut_at_a_page_break(app, monkeypatch):
    import re

    from bananawiki.wiki.features.page_export import pdf

    notes = []
    write_note = pdf._write_cut_note
    monkeypatch.setattr(pdf, "_write_cut_note", lambda document, note: (notes.append(note), write_note(document, note)))
    monkeypatch.setattr(pdf, "MAX_PAGES", 3)
    blocks = {
        "code": "```\n" + "x\n" * 2_000 + "```",
        "table": "| a | b |\n|---|---|\n" + "\n".join(f"| {n} | [x](/page/x) |" for n in range(1_000)),
        # Past the Markdown budget (MAX_HEADINGS) the whole page is one plain-text block.
        "headings": "\n\n".join(f"# h{n}" for n in range(1_500)),
    }
    for name, content in blocks.items():
        notes.clear()
        with app.test_request_context():
            data = pdf.render(title=name, content=content, site_name="Wiki", base_url="https://wiki.example/",
                              meta=[], cut_note="Cut here")
        assert notes == ["Cut here"], name
        assert len(re.findall(rb"/Type /Page\b(?!s)", data)) <= 4, name
        assert all(_saves_and_restores(stream)[0] == _saves_and_restores(stream)[1]
                   for stream in _page_streams(data)), name


def test_qw2_2_plain_text_fallback_keeps_the_layout_deadline(app, monkeypatch):
    from bananawiki.wiki.features.page_export import pdf

    deadlines = []
    write_plain = pdf._write_plain

    def broken(document, *args, **kwargs):
        deadlines.append(document.deadline)
        raise ValueError("unsupported nesting")

    def plain(document, source):
        deadlines.append(document.deadline)
        write_plain(document, source)

    monkeypatch.setattr(pdf, "_write_body", broken)
    monkeypatch.setattr(pdf, "_write_plain", plain)
    with app.test_request_context():
        time.sleep(0.01)
        pdf.render(title="Plain", content="Some text.", site_name="Wiki", base_url="https://wiki.example/", meta=[])
    assert len(deadlines) == 2 and deadlines[1] == deadlines[0]


def _page_streams(data: bytes) -> list[bytes]:
    """The decompressed content streams of the pages of *data* (fpdf2 writes one per page)."""
    import re
    import zlib

    streams = []
    for match in re.finditer(rb"<<([^>]*?)/Filter /FlateDecode([^>]*?)>>\s*stream\r?\n", data):
        start = match.end()
        end = data.index(b"endstream", start)
        try:
            streams.append(zlib.decompress(data[start:end].rstrip(b"\r\n")))
        except zlib.error:
            continue
    return [stream for stream in streams if b" Tf" in stream or b" re" in stream]


def _saves_and_restores(stream: bytes) -> tuple[int, int]:
    """The ``q`` (save graphics state) and ``Q`` (restore) operators in a page's content stream."""
    import re

    return (len(re.findall(rb"(?<![^\s])q(?=\s)", stream)), len(re.findall(rb"(?<![^\s])Q(?=\s)", stream)))


def test_qw2_2_a_cut_at_a_paragraph_end_closes_the_graphics_states(app, monkeypatch):
    from bananawiki.wiki.features.page_export import pdf

    mixed = ("# Heading\n\nSome text with **bold** and `code`.\n\n- one\n- two\n\n> A quote.\n\n"
             "```\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n" + "A paragraph of plain words. " * 8 + "\n\n")
    cuts = 0
    for pages in (2, 3, 5, 7, 11):
        monkeypatch.setattr(pdf, "MAX_PAGES", pages)
        notes = []
        write_note = pdf._write_cut_note
        monkeypatch.setattr(pdf, "_write_cut_note",
                            lambda document, note, write_note=write_note, notes=notes: (notes.append(note),
                                                                                         write_note(document, note)))
        with app.test_request_context():
            data = pdf.render(title=f"Mixed {pages}", content=mixed * 30, site_name="Wiki",
                              base_url="https://wiki.example/", meta=[])
        cuts += len(notes)
        streams = _page_streams(data)
        assert streams, pages
        for stream in streams:
            saves, restores = _saves_and_restores(stream)
            assert saves == restores, (pages, saves, restores)
    assert cuts == 5


def test_qw2_2_a_pdf_cut_by_time_is_laid_out_again_later(app, monkeypatch):
    from bananawiki.wiki.features.page_export import pdf

    monkeypatch.setattr(pdf, "_cache", type(pdf._cache)())
    monkeypatch.setattr(pdf, "_cache_size", 0)
    monkeypatch.setattr(pdf, "_cache_expiry", {}, raising=False)
    layouts = []
    render = pdf._render
    monkeypatch.setattr(pdf, "_render", lambda **kwargs: (layouts.append(1), render(**kwargs))[1])
    clock = [1_000.0]
    monkeypatch.setattr(pdf, "_now", lambda: clock[0], raising=False)
    kept = getattr(pdf, "TIME_CUT_CACHE_SECONDS", 300)

    def export(title: str) -> bytes:
        with app.test_request_context():
            return pdf.render(title=title, content=_paragraphs(300), site_name="Wiki",
                              base_url="https://wiki.example/", meta=[], cut_note="Cut here")

    # Cut by time (a busy CPU, say): kept for a while only, then laid out again.
    monkeypatch.setattr(pdf, "LAYOUT_SECONDS", -1.0)
    export("Slow")
    export("Slow")
    assert len(layouts) == 1
    clock[0] += kept + 1
    monkeypatch.setattr(pdf, "LAYOUT_SECONDS", 8.0)
    export("Slow")
    assert len(layouts) == 2
    # Cut by pages, or complete: kept as before.
    monkeypatch.setattr(pdf, "MAX_PAGES", 2)
    export("Long")
    clock[0] += kept + 1
    export("Long")
    export("Slow")
    assert len(layouts) == 3
