"""Rendering budget, render cache, post-sanitising rewrites and style values (2.x audit)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from markdown.extensions import codehilite

from bananawiki.wiki import markdown

DENSE_LINE = "a+b-c*d/e%f&g|h^i<j>k!l~m?n:o;p,q.r(s)t[u]v{w}x=y\n"


@pytest.fixture(autouse=True)
def _fresh_cache():
    markdown.clear_render_cache()
    yield
    markdown.clear_render_cache()


@pytest.fixture
def highlighted(monkeypatch):
    """Characters of code handed to Pygments by every highlighted block."""
    sizes: list[int] = []
    original = codehilite.highlight

    def counting(source, lexer, formatter):
        sizes.append(len(source))
        return original(source, lexer, formatter)

    monkeypatch.setattr(codehilite, "highlight", counting)
    return sizes


# ── Highlighting budget ──────────────────────────────────────────────────────


def test_fenced_code_highlighting_has_a_per_document_budget(highlighted):
    block = "```c\n" + DENSE_LINE * 10 + "```\n\n"
    source = block * 1500
    html = markdown.render(source[:999_000], mentions=False)
    total = sum(highlighted) + markdown.HIGHLIGHT_BLOCK_COST * len(highlighted)
    assert highlighted, "the first blocks are still highlighted"
    assert total <= markdown.MAX_HIGHLIGHT_DOCUMENT_CHARS
    # Blocks past the budget stay code, as escaped text.
    assert html.count("<pre") >= 1000 and "<span" in html
    assert html.count("a+b-c*d/e%f&amp;g") == 10 * (1500 - len(highlighted))


def test_many_tiny_blocks_spend_the_budget_too(highlighted):
    markdown.render("```c\nx\n```\n\n" * 2400, mentions=False)
    assert len(highlighted) * (1 + markdown.HIGHLIGHT_BLOCK_COST) <= markdown.MAX_HIGHLIGHT_DOCUMENT_CHARS
    assert len(highlighted) < 2400


def test_indented_code_shares_the_budget(highlighted):
    line = "    value = compute(alpha, beta) + 1\n"
    block = "    #!python\n" + line * 40 + "\ntext\n\n"
    html = markdown.render(block * 200, mentions=False)
    assert sum(highlighted) + markdown.HIGHLIGHT_BLOCK_COST * len(highlighted) <= markdown.MAX_HIGHLIGHT_DOCUMENT_CHARS
    assert 0 < len(highlighted) < 200
    assert html.count("value = compute(alpha, beta) + 1") == 40 * (200 - len(highlighted))


def test_ordinary_code_is_still_highlighted(highlighted):
    html = markdown.render("```python\nprint('hi')\n```\n\n    #!python\n    x = 1\n", mentions=False)
    assert len(highlighted) == 2
    assert '<span class="nb">print</span>' in html


def test_blocks_of_one_document_share_one_budget(highlighted, monkeypatch):
    """A page made of many blocks (the page builder) cannot multiply the allowance."""
    codes: list[int] = []
    original = markdown.highlight
    monkeypatch.setattr(markdown, "highlight", lambda source, *a: codes.append(len(source)) or original(source, *a))
    text = "```c\n" + DENSE_LINE * 38 + "```\n"
    code = DENSE_LINE * 40
    with markdown.shared_highlighting():
        texts = [markdown.render(text + f"\n{number}\n") for number in range(60)]
        snippets = [markdown.highlight_code(code + f"{number}\n", "c") for number in range(60)]
    spent = sum(highlighted) + sum(codes) + markdown.HIGHLIGHT_BLOCK_COST * (len(highlighted) + len(codes))
    assert spent <= markdown.MAX_HIGHLIGHT_DOCUMENT_CHARS
    assert "<span" in texts[0] and "<span" not in texts[-1]
    assert codes == [] and "<span" not in snippets[0] and "a+b-c*d/e%f&amp;g" in snippets[0]
    # What the shared allowance left plain is not kept: alone it is highlighted in full.
    assert "<span" in markdown.render(text + "\n59\n")
    assert "<span" in markdown.highlight_code(code + "59\n", "c")


# ── Render cache ─────────────────────────────────────────────────────────────


def test_repeated_renders_of_a_large_source_come_from_the_cache(monkeypatch):
    calls = []
    original = markdown._markdown.markdown
    monkeypatch.setattr(markdown._markdown, "markdown", lambda *a, **k: calls.append(1) or original(*a, **k))
    source = "# Title\n\n" + "Some *text* for @someone.\n\n" * 400
    first = markdown.render(source)
    assert markdown.render(source) == first
    assert len(calls) == 1
    # Options are part of the key.
    markdown.render(source, mentions=False)
    assert len(calls) == 2
    # A custom profile link is never served from (or stored in) the cache.
    custom = markdown.render(source, profile_url=lambda name: "/people/" + name)
    assert 'href="/people/someone"' in custom and len(calls) == 3
    assert markdown.render(source) == first and len(calls) == 3


def test_short_sources_that_were_highlighted_are_cached_too(highlighted):
    source = "```c\n" + DENSE_LINE * 20 + "```\n"
    assert len(source) < markdown.RENDER_CACHE_MIN_CHARS
    first = markdown.render(source)
    assert markdown.render(source) == first and len(highlighted) == 1


def test_highlighted_snippets_are_cached(monkeypatch):
    calls = []
    original = markdown.highlight
    monkeypatch.setattr(markdown, "highlight", lambda *a: calls.append(1) or original(*a))
    code = DENSE_LINE * 40
    first = markdown.highlight_code(code, "c")
    assert markdown.highlight_code(code, "c") == first and len(calls) == 1
    markdown.highlight_code(code, "python")
    assert len(calls) == 2


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(markdown, "RENDER_CACHE_LIMIT", 200_000)
    for number in range(40):
        markdown.render(f"Page {number}\n\n" + "words and more words\n\n" * 300)
    assert markdown._render_cache.size <= 200_000


# ── Rewrites after sanitising ────────────────────────────────────────────────


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _tags(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = _Tags()
    parser.feed(html)
    return parser.tags


def _old_nh3_quoting(monkeypatch):
    """nh3 0.3.0-0.3.6 left ``<`` and ``>`` unescaped in attribute values."""
    original = markdown.sanitize

    def older(html: str) -> str:
        return re.sub(r'="([^"]*)"', lambda m: '="' + m.group(1).replace("&lt;", "<").replace("&gt;", ">") + '"',
                      original(html))

    monkeypatch.setattr(markdown, "sanitize", older)


@pytest.mark.parametrize("source, title", [
    ('<abbr title="x > @abcd y">hi</abbr>', "x > @abcd y"),
    ('<abbr title="x &gt; @abcd y">hi</abbr>', "x > @abcd y"),
    ('- <abbr title="&lt;li&gt;[ ] x">hi</abbr>', "<li>[ ] x"),
    ('<abbr title="&lt;p&gt;[[video url=&quot;https://youtu.be/dQw4w9WgXcQ&quot;]]&lt;/p&gt;">v</abbr>',
     '<p>[[video url="https://youtu.be/dQw4w9WgXcQ"]]</p>'),
    ('<abbr title="&lt;p&gt;https://youtu.be/dQw4w9WgXcQ&lt;/p&gt;">v</abbr>', "<p>https://youtu.be/dQw4w9WgXcQ</p>"),
    ('<abbr title="&lt;p&gt;[[canvas slug=&quot;plan&quot;]]&lt;/p&gt;">c</abbr>', '<p>[[canvas slug="plan"]]</p>'),
])
@pytest.mark.parametrize("old_nh3", [False, True])
def test_mentions_embeds_and_task_lists_never_rewrite_attribute_values(monkeypatch, source, title, old_nh3):
    if old_nh3:
        _old_nh3_quoting(monkeypatch)
    html = markdown.render(source)
    tags = _tags(html)
    abbrs = [attrs for tag, attrs in tags if tag == "abbr"]
    assert abbrs == [{"title": title}], html
    assert not [tag for tag, _ in tags if tag in {"a", "iframe", "span"}], html
    assert "data-embed" not in html and "task-item" not in html


def test_rewrites_still_apply_to_text():
    html = markdown.render("- [x] done by @abcd\n\n[[canvas slug=\"plan\"]]")
    assert 'class="task-item task-done"' in html
    assert '<a class="mention" href="/users/abcd">@abcd</a>' in html
    assert 'data-embed-ref="plan"' in html


# ── Style values ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("style", [
    "width:100vw;height:100vh;margin-top:-500px",
    "margin-top: -400px",
    "width: calc(100% + 400px)",
    "height: var(--x)",
    "max-width: 99999px",
    "width: 100% !important",
    "padding: -5px",
    "margin-left: 10vh",
])
def test_style_values_that_could_overlay_page_chrome_are_dropped(style):
    html = markdown.render(f'<div style="{style}">x</div>')
    assert "vw" not in html and "vh" not in html and "-" not in html.replace("<div", "")
    assert "calc" not in html and "var(" not in html and "important" not in html and "99999" not in html


@pytest.mark.parametrize("style, kept", [
    ("width: 50%; margin-left: auto; margin-right: auto", "width:50%;margin-left:auto;margin-right:auto"),
    ("margin-top: 10px; padding: 4px 8px", "margin-top:10px;padding:4px 8px"),
    ("max-width: 320px; height: 12.5em", "max-width:320px;height:12.5em"),
    ("--media-aspect-ratio: 16 / 9; --media-max-width: 100%", "--media-aspect-ratio:16 / 9;--media-max-width:100%"),
    ("WIDTH: 20REM; color: red", "width:20rem"),
])
def test_ordinary_layout_styles_are_kept(style, kept):
    html = markdown.render(f'<div style="{style}">x</div>')
    found = re.search(r'style="([^"]*)"', html)
    assert found and found.group(1).replace(" ", "") == kept.replace(" ", ""), html


def test_image_size_styles_are_kept():
    html = markdown.render('<img src="/uploads/a.png" alt="a" style="width: 240px; height: auto">')
    assert 'style="width:240px;height:auto"' in html.replace(": ", ":").replace("; ", ";")
