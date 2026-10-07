"""Content accepted under the byte limit must not exhaust a render worker."""

import random
import re
import subprocess
import sys
from html import escape

import markdown as python_markdown
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
    # Each pair of fences closes earlier than it looks, leaving the body text.
    "misaligned_indented_closer_headings": "```\n  ```\n```\n" + "#a\n" * 20_000 + "```",
    "misaligned_indented_closer_brackets": "```\n  ```\n```\n" + "a[" * 20_000 + "\n```",
    "misaligned_tilde_closer_headings": "```\n~~~\n```\n" + "#a\n" * 20_000 + "```",
    "misaligned_tilde_closer_brackets": "```\n~~~\n```\n" + "a[" * 20_000 + "\n```",
    "misaligned_fence_length_headings": "````\n```\n````\n" + "#a\n" * 20_000 + "````",
    "misaligned_fence_length_brackets": "````\n```\n````\n" + "a[" * 20_000 + "\n````",
    "fence_with_control_character_headings": "```\n``\x02`\n" + "#a\n" * 20_000 + "```",
    "fence_with_control_character_brackets": "```\n``\x02`\n" + "a[" * 20_000 + "\n```",
    "unclosed_fence_openings": "```x\n\n" * 20_000,
    "spaces_after_fence": "```" + " " * 20_000 + "x y",
    # Without a closer, a legacy hl_lines value is tried up to every later
    # line that ends in its quote, and the rest is searched again from each.
    "unclosed_hl_lines_quote_ends": '```hl_lines="1"\n\n' + ('"\n' * 20 + "\n") * 2000,
    "unclosed_hl_lines_after_spaces": "```" + " " * 50 + "hl_lines='1'\n\n" + ("'\n" * 20 + "\n") * 500,
    "unconfirmed_fence_segments": ("  ```\n" + "[" * 40 + "\n  ~~~\n") * 300,
    "many_paragraphs": "a\n\n" * 100_000,
    "many_fences": "```\na\n```\n\n" * 5000,
    "many_tables": "|a|\n|-|\n|b|\n\n" * 10_000,
    "unclosed_video_paragraphs": "[[video a\n\n" * 90_000,
    "repeated_toc_markers": "[TOC]\n\n" * 100_000,
    "reference_link_expansion": "[r]: https://example.com/" + "a" * 50_000 + "\n\n" + "[a][r]\n\n" * 2000,
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


_FENCE_LINES = [
    "```", "~~~", "````", "``` ", "  ```", "\t```", "``\x02`", "~~\x03~", "```python", "``` {.py}", "```{.py} ",
    "```{.py}}", "```{a}b}", '```{title="a}b"}', "```{}", '```hl_lines="1"', '```py hl_lines="1 2" x',
    "```hl_lines='", '"', "'", 'x"  ', "```x y", "```     x", "```.c++", "~~~~ ", "```~~~", "a\rb", "",
    "    ", "text", "# heading",
]


def _parser_fences(parser, text):
    """The lines Python-Markdown's fenced_code preprocessor leaves, each block as one placeholder."""
    parser.reset()
    lines = parser.preprocessors["normalize_whitespace"].run(text.split("\n"))
    lines = parser.preprocessors["fenced_code_block"].run(lines)
    return ["<code>" if re.fullmatch("\x02wzxhzdk:\\d+\x03", line) else line for line in lines]


def _fences_found_where_the_parser_finds_them(parser, text):
    lines = markdown._normalise_whitespace(text).split("\n")
    blocks, _, unclosed = markdown._fenced_code(lines)
    found, start = [], 0
    for first, last in blocks:
        found += [*lines[start:first], "", "<code>", ""]
        start = last + 1
    assert found + lines[start:] == _parser_fences(parser, text), text
    return lines, blocks, unclosed


def test_fenced_code_is_found_exactly_where_the_parser_finds_it():
    # Text taken for code escaped the work checks, and fences that look
    # paired often are not.
    parser = python_markdown.Markdown(extensions=["fenced_code"])
    rng = random.Random(1606)
    documents = ["```\n  ```\n```\nbody\n```", "```\n~~~\n```\nbody\n```", "````\n```\n````\nbody\n````",
                 "```\n``\x02`\nbody\n```", '```py hl_lines="1\n"\n~~~\n```\nbody\n~~~']
    documents += ["\n".join(rng.choice(_FENCE_LINES) for _ in range(rng.randint(1, 30))) for _ in range(2000)]
    for text in documents:
        unclosed = _fences_found_where_the_parser_finds_them(parser, text)[2]
        if unclosed:
            # Excerpts append this fence to a cut page so its code stays code.
            lines, blocks, _ = _fences_found_where_the_parser_finds_them(parser, text + "\n" + unclosed)
            assert blocks[-1][1] == len(lines) - 3, text
    # An excerpt closes the fence the parser leaves open, not the one that
    # looks open nor one the parser would skip.
    assert markdown._source_head("~~~\n```\n" + "x\n" * 3000, 4096).endswith("\nx\n~~~")
    assert markdown._source_head("```\n  ```\n" + "x\n" * 3000, 4096).endswith("\nx\n```")
    assert markdown._source_head("````{a}b}\n```\n" + "x\n" * 3000, 4096).endswith("\nx\n```")


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


def test_blocks_inside_a_skipped_fence_keep_the_highlighting_bound():
    # The parser skips an opening with malformed attributes, then finds the
    # block inside it; that block is bounded like any other.
    code = "x = 1\n\n" * 10_000
    assert len(code) > markdown.MAX_HIGHLIGHT_CODE_CHARS
    html = render("```{a}b}\n```python\n" + code + "```", mentions=False)
    assert html.count("x = 1") == 10_000
    assert "<span" not in html


def test_large_direct_code_highlighting_is_literal_and_bounded(monkeypatch):
    def unexpected_guess(_source):
        pytest.fail("large code must not run automatic lexer detection")

    monkeypatch.setattr(markdown, "guess_lexer", unexpected_guess)
    code = 'print("<script>")\n' * 10_000
    assert markdown.highlight_code(code) == "<pre><code>" + escape(code) + "</code></pre>"


def _render_with_deadline(source):
    # A subprocess deadline protects the suite if a bound regresses.
    return subprocess.run([
        sys.executable, "-c",
        "import sys; from bananawiki.wiki.markdown import render; "
        "sys.stdout.write(render(sys.stdin.read(), mentions=False))",
    ], input=source, capture_output=True, text=True, timeout=15, check=True).stdout


_UNCLOSED_SHORTCODES = {
    "spaces_after_keyword": "[[video" + " " * 200_000 + "x",
    "unclosed_video_paragraphs": "[[video a\n\n" * 10_000,
    "unclosed_board_paragraphs": "[[kanban a\n\n" * 10_000,
    "long_attribute_words": ("[[video " + "a" * 1990 + "]]\n\n") * 200,
    "oversized_attributes": "[[video " + "a" * 100_000 + "]]",
    "unclosed_inline_markup_paragraphs": "[[video *a* `b`\n\n" * 10_000,
}


@pytest.mark.parametrize("source", list(_UNCLOSED_SHORTCODES.values()), ids=list(_UNCLOSED_SHORTCODES))
def test_unclosed_and_oversized_shortcodes_stay_literal_in_linear_time(source):
    html = _render_with_deadline(source)
    assert html.startswith("<p>[[")
    assert "bw-video" not in html and "bw-embed" not in html


_REPEATED_WATCH_URL = "https://youtube.com/watch?" + "youtube.com/watch?" * 55_000


@pytest.mark.parametrize(("source", "prefix"), [
    (_REPEATED_WATCH_URL, "<p>https://youtube.com/watch?youtube.com/"),
    ("<" + _REPEATED_WATCH_URL + ">", '<p><a href="https://youtube.com/watch?youtube.com/'),
], ids=["bare", "autolink"])
def test_overlong_video_links_stay_links_in_linear_time(source, prefix):
    # Player detection used to rescan the rest of the URL from every "watch?".
    assert len(source) < markdown.MAX_SOURCE_CHARS
    html = _render_with_deadline(source)
    assert html.startswith(prefix) and "bw-video" not in html


def test_video_urls_are_recognised_up_to_the_length_limit():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&si="
    url += "L" * (markdown.MAX_VIDEO_URL_CHARS - len(url))
    assert markdown.video_embed_src(url) == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
    assert markdown.video_embed_src(url + "L") is None


def test_shortcodes_keep_line_breaks_and_long_urls():
    url = "https://www.youtube.com/embed/dQw4w9WgXcQ?si="
    url += "L" * (markdown.MAX_VIDEO_URL_CHARS - len(url))
    options = 'align="left" ratio="4:3" width="640" margin="20" autoplay="true" loop="true" controls="false"'
    html = render(f'[[video url="{url}"\n{options}]]\n\n[[kanban board="7"\nheight="300"]]', mentions=False)
    assert "youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1" in html
    assert "bw-video-left" in html and "ratio-4x3" in html and 'data-width="640"' in html
    assert 'data-embed-ref="7" data-height="300"' in html


def test_shortcodes_embed_when_their_paragraph_holds_inline_markup():
    # "_abc_" in the URL becomes <em>abc</em> before shortcodes are read.
    html = render('[[video url="https://youtu.be/dQw4w9WgXcQ?si=_abc_"]]\n\n[[kanban board="7" note="*x*"]]',
                  mentions=False)
    assert "youtube-nocookie.com/embed/dQw4w9WgXcQ" in html and 'data-embed-ref="7"' in html


def test_embeds_per_document_are_capped_and_the_rest_stay_literal():
    video = '[[video url="https://youtu.be/dQw4w9WgXcQ"]]\n\n'
    board = '[[kanban board="7"]]\n\n'
    html = render(video * (markdown.MAX_EMBEDS - 1) + board * 2, mentions=False)
    assert html.count('<div class="bw-video') == markdown.MAX_EMBEDS - 1
    assert html.count('<div class="bw-embed') == 1 and html.count("<p>[[kanban board=") == 1
    html = render(video * (markdown.MAX_EMBEDS + 1) + board + "https://vimeo.com/123", mentions=False)
    assert html.count('<div class="bw-video') == markdown.MAX_EMBEDS
    assert html.count("<p>[[video url=") == 1 and "<p>[[kanban board=" in html
    assert "bw-embed" not in html and "player.vimeo.com" not in html


def test_only_the_first_toc_marker_becomes_a_table_of_contents():
    headings = "".join(f"## Section {number}\n\n" for number in range(markdown.MAX_HEADINGS))
    # 13 KB of source used to produce about 48 MB of HTML.
    html = _render_with_deadline("[TOC]\n\n" + headings + "[TOC]\n\n" * 800)
    assert html.count('<div class="toc">') == 1
    assert html.count('href="#section-999"') == 1
    assert html.count("<p>[TOC]</p>") == 800
    assert len(html) < 200_000


def test_block_allowance_leaves_ordinary_documents_alone():
    paragraph = "A paragraph of text.\n\n"
    assert not markdown._parser_work_exceeded(paragraph * (markdown.MAX_BLOCK_COST - 1))
    assert markdown._parser_work_exceeded(paragraph * (markdown.MAX_BLOCK_COST + 1))
    # Highlighted code is weighed as several paragraphs.
    section = paragraph + "```python\nx = 1\n```\n\n"
    assert not markdown._parser_work_exceeded(section * 2000)
    assert markdown._parser_work_exceeded(section * 2500)
    # A fence left open is searched for to the end of the page, once.
    assert not markdown._parser_work_exceeded("```python\n\n" + paragraph * 20_000)
    # With a legacy hl_lines value, once more from every later line ending in
    # its quote: 50 of them cost about 0.4 s of searching on a 440 KB page.
    opening, quoted = '```python hl_lines="1"\n\n', 'He said "hi"\n\n'
    assert not markdown._parser_work_exceeded(opening + quoted * 50 + paragraph * 2000)
    assert markdown._parser_work_exceeded(opening + quoted * 50 + paragraph * 20_000)


def test_ordinary_reference_links_still_render():
    source = "[r]: https://example.com/" + "a" * 100 + "\n\n" + "[a][r]\n\n" * 50
    assert render(source, mentions=False).count('href="https://example.com/' + "a" * 100 + '"') == 50


def test_excerpt_renders_only_the_start_of_long_pages(monkeypatch):
    rendered = []
    plain_text = markdown.to_plain_text

    def recording(text):
        rendered.append(len(text))
        return plain_text(text)

    monkeypatch.setattr(markdown, "to_plain_text", recording)
    body = "A paragraph with **bold** text and a [link](https://example.com/).\n\n" * 13_000
    expected = plain_text(body[:20_000])[:159].rstrip() + "…"
    assert markdown.excerpt(body, 160) == expected
    assert rendered and max(rendered) <= markdown.EXCERPT_SOURCE_CHARS

    rendered.clear()
    # Markdown releases differ on unclosed raw HTML (text or dropped); only the work is checked.
    assert len(markdown.excerpt("<a " * 330_000, 160)) <= 160 and rendered
    assert markdown.excerpt("Text\n\n    code\n\n" * 60_000, 160).startswith("Text code Text code")
    assert max(rendered, default=0) <= markdown.EXCERPT_SOURCE_CHARS


def test_excerpt_weighs_the_line_breaks_that_blank_runs_become(monkeypatch):
    plain_text = markdown.to_plain_text
    assert markdown.excerpt("Intro\n\n\n\n\nMore *text*.", 160) == plain_text("Intro\n\n\n\n\nMore *text*.")
    rendered = []
    monkeypatch.setattr(markdown, "to_plain_text", lambda text: rendered.append(text) or plain_text(text))
    # Each preserved blank line becomes a <br> tag inside a single paragraph.
    assert markdown.excerpt("\n" * 2802 + "x", 160) == ""
    assert markdown.excerpt("\n" * 600 + "Visible text", 160) == "Visible text"
    assert rendered == []


@pytest.mark.parametrize("source", [
    "#a\n#a\n#a\n#a\n\n" * 250 + "tail",
    "a\n-\n" * 1000,
], ids=["atx", "setext"])
def test_excerpt_weighs_repeated_headings(monkeypatch, source):
    # Each repeated title searches every earlier one for a free id: 1000 of
    # them in 4 KB cost half a second per excerpt.
    assert not markdown._parser_work_exceeded("#a\n" * markdown.MAX_EXCERPT_HEADINGS,
                                              heading_limit=markdown.MAX_EXCERPT_HEADINGS)
    assert markdown._parser_work_exceeded("#a\n" * (markdown.MAX_EXCERPT_HEADINGS + 1),
                                          heading_limit=markdown.MAX_EXCERPT_HEADINGS)
    rendered = []
    plain_text = markdown.to_plain_text
    monkeypatch.setattr(markdown, "to_plain_text", lambda text: rendered.append(text) or plain_text(text))
    preview = markdown.excerpt(source, 160)
    assert preview.startswith(" ".join(source.split()[:8])) and preview.endswith("…")
    assert rendered == []


_MISALIGNED_FENCES = {
    "indented_closer": ("```\n  ```\n```\n", "```"),
    "tilde_closer": ("```\n~~~\n```\n", "```"),
    "longer_fence": ("````\n```\n````\n", "````"),
    "control_character": ("```\n``\x02`\n", "```"),
}


@pytest.mark.parametrize("body", ["#a\n" * 1000, ("a[" * 30 + "\n") * 60], ids=["headings", "brackets"])
@pytest.mark.parametrize(("opening", "closing"), list(_MISALIGNED_FENCES.values()), ids=list(_MISALIGNED_FENCES))
def test_excerpt_allowances_cover_text_between_misaligned_fences(monkeypatch, opening, closing, body):
    # The first two fences close each other, so the body is text. Taken for
    # code, it rendered 4 KB starts at half a second or more each.
    source = opening + body + closing
    assert len(source) < markdown.EXCERPT_SOURCE_CHARS
    rendered = []
    plain_text = markdown.to_plain_text
    monkeypatch.setattr(markdown, "to_plain_text", lambda text: rendered.append(text) or plain_text(text))
    assert markdown.excerpt(source, 160).endswith("…")
    assert rendered == []


_UNCLOSED_HL_LINES_BODY = ('"\n' * 10 + "\n") * 4000


@pytest.mark.parametrize("source", [
    ('```hl_lines="1"\n\n' + _UNCLOSED_HL_LINES_BODY)[:4090],
    ("```" + " " * 100 + 'hl_lines="1"\n\n' + _UNCLOSED_HL_LINES_BODY)[:4090],
    '````{a}b}\n```hl_lines="1"\n\n' + _UNCLOSED_HL_LINES_BODY,
], ids=["short_page", "spaces_before_value", "after_a_skipped_fence"])
def test_excerpt_allowances_cover_hl_lines_values_that_never_close(monkeypatch, source):
    # Each later line ending in the quote restarted the search for a closer:
    # such a 4 KB start took eight seconds per excerpt.
    rendered = []
    plain_text = markdown.to_plain_text
    monkeypatch.setattr(markdown, "to_plain_text", lambda text: rendered.append(text) or plain_text(text))
    assert markdown.excerpt(source, 160).endswith("…")
    # Only a start whose fence the excerpt closed may render.
    assert all(markdown._fenced_code(markdown._normalise_whitespace(text).split("\n"))[2] is None
               for text in rendered)


@pytest.mark.parametrize("source", [
    "```python\n" + "\n".join(f"value_{number} = compute({number})" for number in range(2000)) + "\n```\n\nAfter.",
    "- item\n" * 1000,
    "".join(f"## Topic {number}\n" for number in range(190)),
    "Short page with *emphasis*.",
], ids=["long_opening_code", "dense_list", "heading_outline", "short_page"])
def test_excerpt_matches_the_whole_page_preview(source):
    plain = markdown.to_plain_text(source)
    expected = plain if len(plain) <= 160 else plain[:159].rstrip() + "…"
    assert markdown.excerpt(source, 160) == expected
