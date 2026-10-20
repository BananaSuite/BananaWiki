"""Plain text and excerpts never highlight code.

Highlighted code is stripped of its markup right away, so highlighting it
would only cost time and, inside :func:`markdown.shared_highlighting`, the
allowance of the page that lists the excerpts (a page-builder page list).
"""

from __future__ import annotations

import pytest
from markdown.extensions import codehilite

from bananawiki.wiki import markdown

CODE_PAGE = "```python\n" + "def f(x):\n    return x * 2\n" * 150 + "```\n\nAfter the code."


@pytest.fixture(autouse=True)
def _fresh_cache():
    markdown.clear_render_cache()
    yield
    markdown.clear_render_cache()


@pytest.fixture
def highlighted(monkeypatch):
    """Characters of code handed to Pygments."""
    sizes: list[int] = []
    original = codehilite.highlight

    def counting(source, lexer, formatter):
        sizes.append(len(source))
        return original(source, lexer, formatter)

    monkeypatch.setattr(codehilite, "highlight", counting)
    return sizes


def test_excerpts_and_plain_text_never_highlight_code(highlighted):
    assert markdown.excerpt(CODE_PAGE, 60) == "def f(x): return x * 2 def f(x): return x * 2 def f(x): ret…"
    assert markdown.to_plain_text(CODE_PAGE).startswith("def f(x): return x * 2 def f(x)")
    assert markdown.to_plain_text(CODE_PAGE).endswith("After the code.")
    assert highlighted == []


def test_excerpts_leave_the_shared_allowance_to_the_page(highlighted):
    with markdown.shared_highlighting():
        excerpts = [markdown.excerpt(f"{CODE_PAGE}\n{n}", 160) for n in range(20)]
        html = markdown.highlight_code("def f(x):\n    return x\n", "python")
        page = markdown.render("```python\nvalue = compute(1)\n```")
    assert all(text.startswith("def f(x): return x * 2") for text in excerpts)
    assert 'class="codehilite"' in html and 'class="codehilite"' in page


def test_plain_text_is_cached_apart_from_the_highlighted_rendering(highlighted):
    source = "```python\nvalue = compute(1)\n```\n\n" + "words " * 400
    html = markdown.render(source)
    assert 'class="codehilite"' in html and len(highlighted) == 1
    assert markdown.to_plain_text(source).startswith("value = compute(1) words")
    assert markdown.render(source) == html and len(highlighted) == 1
