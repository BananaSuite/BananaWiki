"""Content accepted under the byte limit must not exhaust a render worker."""

import subprocess
import sys
from html import escape

import pytest

from bananawiki.wiki import markdown
from bananawiki.wiki.markdown import render

_PATHOLOGICAL = {
    "unmatched_brackets": "[" * 20_000,
    "balanced_brackets": "[" * 1000 + "body" + "]" * 1000,
    "flat_unmatched_links": "[a](" * 3000,
    "nested_parentheses": "[link](https://example.com/" + "(" * 1000 + "x" + ")" * 1001,
    "nested_lists": "\n".join("    " * level + "- item" for level in range(200)),
    "nested_quotes": "> " * 2000 + "body",
    "nested_emphasis": "*a _b " * 3000 + "body" + " b_ a*" * 3000,
    "unclosed_fence": "```\n" + "a[" * 20_000,
    "mismatched_fence": "```\n" + "a[" * 20_000 + "\n~~~",
    "mismatched_fence_length": "````\n" + "a[" * 20_000 + "\n```",
    "indented_fence": "  ```\n" + "a[" * 20_000 + "\n  ```",
    "nested_html": "<div>" * 10_000 + "<script>alert(1)</script>" + "</div>" * 10_000,
    "strikethrough": "~~x~~ " * 12_000,
    "entities": "&amp;" * 40_000,
    "preserved_blank_lines": "before" + "\n" * 20_000 + "after",
    "escaped_backslashes": "\\" * 100_000,
    "line_breaks": "text  \n" * 20_000,
    "repeated_headings": "# heading\n\n" * 15_000,
    "repeated_setext_headings": "heading\n---\n\n" * 15_000,
    "quoted_headings": "> # heading\n\n" * 15_000,
    "list_headings": "- # heading\n\n" * 15_000,
    "mixed_prefix_headings": "> - > # heading\n\n" * 2500,
    "nested_list_headings": "- - # heading\n\n" * 2500,
    "setext_trailing_space": "heading\n--- \n\n" * 2500,
    "setext_trailing_tab": "heading\n===\t\n\n" * 2500,
    "invalid_fence_attrs": "```{.python }}\n\n" + "a[" * 12_000 + "\n\n```",
    "unconfirmed_fence_segments": ("  ```\n" + "[" * 40 + "\n  ~~~\n") * 300,
}


@pytest.mark.parametrize("source", list(_PATHOLOGICAL.values()), ids=list(_PATHOLOGICAL))
def test_pathological_markdown_has_a_bounded_escaped_fallback(source):
    # A subprocess deadline protects the suite if the parser guard regresses.
    result = subprocess.run([
        sys.executable, "-c",
        "import sys; from bananawiki.wiki.markdown import render; "
        "sys.stdout.write(render(sys.stdin.read(), mentions=False))",
    ], input=source, capture_output=True, text=True, timeout=5, check=True)
    assert result.stdout == "<pre>" + escape(source) + "</pre>"


def test_large_ordinary_prose_preserves_its_formatting():
    source = "## Heading\n\n" + "A **readable paragraph** with a [link](https://example.com/).\n\n" * 2000
    html = render(source, mentions=False)
    assert '<h2 id="heading">Heading</h2>' in html
    assert html.count("<strong>readable paragraph</strong>") == 2000
    assert html.count('href="https://example.com/"') == 2000
    assert "<pre>" not in html


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_large_fenced_code_is_literal_and_retains_its_contents(fence):
    code = "unmatched = [\n" * 10_000
    source = fence + "\n" + code + fence
    html = render(source, mentions=False)
    assert "<pre>" in html and "<code>" in html
    assert html.count("unmatched = [") == 10_000
    assert fence not in html


def test_normal_links_lists_emphasis_and_code_remain_compatible():
    html = render("[nested [label]](https://example.com/a(b))\n\n"
                  "- First **bold _nested emphasis_**\n  - Next\n\n"
                  "`literal " + "[" * 100 + "`", mentions=False)
    assert '<a href="https://example.com/a(b)"' in html
    assert "nested [label]</a>" in html
    assert "<strong>bold <em>nested emphasis</em></strong>" in html
    assert "<ul>" in html
    assert "<code>literal " + "[" * 100 + "</code>" in html


def test_fence_display_options_cannot_become_unbounded_pygments_arguments(monkeypatch):
    from markdown.extensions import fenced_code

    original = fenced_code.CodeHilite
    observed = []

    def checked_highlighter(*args, **kwargs):
        # Reject a regression before any oversized tab expansion can run.
        assert kwargs["tabsize"] <= 16
        assert kwargs["linenostart"] <= markdown.MAX_SOURCE_CHARS
        assert kwargs["guess_lang"] is False
        assert kwargs["style"] == "default"
        assert "encoding" not in kwargs
        assert len(kwargs["hl_lines"]) <= markdown.MAX_HIGHLIGHT_LINES
        observed.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(fenced_code, "CodeHilite", checked_highlighter)
    highlights = " ".join(map(str, range(1, 600))) + " 9999999999999999999999999 -1 bad"
    source = ('```{.python tabsize=1000000000 linenostart=9999999999 guess_lang=true '
              'encoding=invalid style=missing-style pygments_style=missing-style '
              f'hl_lines="{highlights}"}}\n\tprint("hello")\n```')
    html = render(source, mentions=False)
    assert observed
    assert '<span class="nb">print</span>' in html
    assert "hello" in html
    assert len(html) < 5000


@pytest.mark.parametrize("options", ["tabsize=bad", "linenostart=bad", "encoding=invalid", "style=missing",
                                   "pygments_style=missing", "linenos=bad", "guess_lang=true"])
def test_invalid_fence_options_degrade_without_render_errors(options):
    html = render(f'```{{.python {options}}}\nprint("hello")\n```', mentions=False)
    assert '<span class="nb">print</span>' in html
    assert "hello" in html


@pytest.mark.parametrize("legacy", [False, True])
def test_highlight_line_list_is_bounded_in_both_fence_syntaxes(monkeypatch, legacy):
    from markdown.extensions import fenced_code

    original = fenced_code.CodeHilite
    observed = []

    def checked_highlighter(*args, **kwargs):
        observed.append(kwargs["hl_lines"])
        assert len(kwargs["hl_lines"]) <= markdown.MAX_HIGHLIGHT_LINES
        return original(*args, **kwargs)

    monkeypatch.setattr(fenced_code, "CodeHilite", checked_highlighter)
    highlights = " ".join(map(str, range(1, 600)))
    opening = f'```python hl_lines="{highlights}"' if legacy else f'```{{.python hl_lines="{highlights}"}}'
    html = render(opening + '\nprint("hello")\n```', mentions=False)
    assert '<span class="nb">print</span>' in html
    assert observed and len(observed[0]) == markdown.MAX_HIGHLIGHT_LINES


def test_auto_lexer_detection_uses_a_bounded_sample(monkeypatch):
    from pygments.lexers import TextLexer

    seen = []

    def guess(sample):
        seen.append(len(sample))
        return TextLexer()

    monkeypatch.setattr(markdown, "guess_lexer", guess)
    code = "readable code\n" * 2000
    html = markdown.highlight_code(code)
    assert seen == [markdown.MAX_LEXER_SAMPLE_CHARS]
    assert html.count("readable code") == 2000


@pytest.mark.parametrize("attrs", ["python", "{.python use_pygments=true}"])
def test_large_known_language_fences_keep_code_without_span_amplification(attrs):
    code = "x = 1\n" * 100_000
    result = subprocess.run([
        sys.executable, "-c",
        "import sys; from bananawiki.wiki.markdown import render; "
        "sys.stdout.write(render(sys.stdin.read(), mentions=False))",
    ], input=f"```{attrs}\n{code}```", capture_output=True, text=True, timeout=5, check=True)
    assert "<pre>" in result.stdout and "<code" in result.stdout
    assert result.stdout.count("x = 1") == 100_000
    assert "<span" not in result.stdout
    assert len(result.stdout) < len(code) + 100


def test_large_direct_code_highlighting_is_literal_and_bounded(monkeypatch):
    def unexpected_guess(_source):
        pytest.fail("large code must not run automatic lexer detection")

    monkeypatch.setattr(markdown, "guess_lexer", unexpected_guess)
    code = 'print("<script>")\n' * 10_000
    assert markdown.highlight_code(code) == "<pre><code>" + escape(code) + "</code></pre>"
