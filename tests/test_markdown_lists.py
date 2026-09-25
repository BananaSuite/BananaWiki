"""
Tests for Markdown list rendering, in particular the auto-blank-line
preprocessor that ensures a list directly following a paragraph is
recognised as a list rather than absorbed into the paragraph.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestNumberedListAfterParagraph:
    """Numbered (ordered) lists must render as ``<ol>`` even when the
    user forgets the blank line between the paragraph and the first
    list item."""

    def test_list_at_start_of_document(self):
        from helpers._markdown import render_markdown
        md = "1. one\n2. two\n3. three"
        html = render_markdown(md)
        assert "<ol>" in html
        assert html.count("<li>") == 3
        assert "1. one" not in html

    def test_list_directly_after_paragraph(self):
        from helpers._markdown import render_markdown
        md = "Here is a list:\n1. one\n2. two\n3. three"
        html = render_markdown(md)
        assert "<p>Here is a list:</p>" in html
        assert "<ol>" in html
        assert html.count("<li>") == 3
        # The numbers must not leak into the paragraph as plain text.
        assert "1. one" not in html
        assert "2. two" not in html

    def test_list_with_blank_line_unchanged(self):
        from helpers._markdown import render_markdown
        md = "Here is a list:\n\n1. one\n2. two\n3. three"
        html = render_markdown(md)
        assert "<p>Here is a list:</p>" in html
        assert "<ol>" in html
        assert html.count("<li>") == 3

    def test_list_after_heading(self):
        from helpers._markdown import render_markdown
        md = "## Heading\n1. one\n2. two"
        html = render_markdown(md)
        assert "<h2" in html
        assert "<ol>" in html
        assert html.count("<li>") == 2


class TestBulletListAfterParagraph:
    """The same auto-blank-line behaviour must apply to bullet lists."""

    def test_bullet_list_directly_after_paragraph(self):
        from helpers._markdown import render_markdown
        md = "Shopping list:\n- apples\n- bananas\n- carrots"
        html = render_markdown(md)
        assert "<p>Shopping list:</p>" in html
        assert "<ul>" in html
        assert html.count("<li>") == 3

    def test_asterisk_bullet_list_directly_after_paragraph(self):
        from helpers._markdown import render_markdown
        md = "Things:\n* a\n* b"
        html = render_markdown(md)
        assert "<ul>" in html
        assert html.count("<li>") == 2


class TestListPreprocessorSafety:
    """The preprocessor must not corrupt content that already renders
    correctly or content that contains numbers in non-list contexts."""

    def test_fenced_code_block_is_not_touched(self):
        from helpers._markdown import render_markdown
        md = "Example:\n```\n1. one\n2. two\n```"
        html = render_markdown(md)
        assert "<pre>" in html
        assert "<code>" in html
        # The numbers stay verbatim inside the code block.
        assert "1. one" in html
        assert "2. two" in html
        assert "<ol>" not in html

    def test_indented_continuation_does_not_break_list(self):
        from helpers._markdown import render_markdown
        md = "1. first item\n   continuation\n2. second"
        html = render_markdown(md)
        assert "<ol>" in html
        assert html.count("<li>") == 2

    def test_consecutive_list_items_unaffected(self):
        from helpers._markdown import render_markdown
        md = "1. one\n2. two\n3. three"
        html = render_markdown(md)
        # No spurious paragraph break in the middle of the list.
        assert html.count("<ol>") == 1
        assert html.count("</ol>") == 1
        assert html.count("<li>") == 3

    def test_number_in_sentence_is_not_a_list(self):
        from helpers._markdown import render_markdown
        # "1996" has no ".", "*" or ")" delimiter, so it's never a list.
        md = "He was born in 1996 and grew up nearby."
        html = render_markdown(md)
        assert "<ol>" not in html
        assert "<ul>" not in html

    def test_empty_text_returns_empty(self):
        from helpers._markdown import render_markdown
        assert render_markdown("") == ""
        assert render_markdown(None) == ""


class TestNestedListIndentation:
    """Sub-lists indented with 2 spaces (CommonMark / GitHub / Notion /
    VS Code convention) must render as nested ``<ul>``/``<ol>`` rather
    than flatten into a single level.  Lists already using 4-space
    indentation must be left untouched.  Indented code inside a fenced
    code block must never be normalised."""

    def test_two_space_nested_bullet_list(self):
        from helpers._markdown import render_markdown
        md = "- a\n  - b\n  - c\n- d"
        html = render_markdown(md)
        # One outer list, one inner list = two <ul> opens.
        assert html.count("<ul>") == 2
        assert html.count("</ul>") == 2
        assert html.count("<li>") == 4

    def test_two_space_nested_numbered_list(self):
        from helpers._markdown import render_markdown
        md = "1. one\n  1. inner\n  2. inner2\n2. two"
        html = render_markdown(md)
        assert html.count("<ol>") == 2
        assert html.count("<li>") == 4

    def test_three_level_two_space_nesting(self):
        from helpers._markdown import render_markdown
        md = "- a\n  - b\n    - deep\n  - c\n- d"
        html = render_markdown(md)
        # Three nesting levels.
        assert html.count("<ul>") == 3
        assert html.count("<li>") == 5

    def test_four_space_nesting_is_unchanged(self):
        from helpers._markdown import render_markdown
        md = "- a\n    - b\n    - c\n- d"
        html = render_markdown(md)
        assert html.count("<ul>") == 2
        assert html.count("<li>") == 4

    def test_two_space_indent_inside_fenced_code_is_not_normalised(self):
        from helpers._markdown import render_markdown
        md = "```\n- a\n  - b\n```"
        html = render_markdown(md)
        # Code block content stays verbatim. No real nested list emitted.
        assert "<pre>" in html
        assert "  - b" in html
        assert "<ul>" not in html

    def test_paragraph_then_two_space_nested_list(self):
        from helpers._markdown import render_markdown
        md = "Intro line.\n\n- a\n  - b\n- c"
        html = render_markdown(md)
        assert "<p>Intro line.</p>" in html
        assert html.count("<ul>") == 2


class TestMultipleNewlines:
    """Two or more blank lines in a row must produce visible vertical
    space in the rendered output, not collapse to a single paragraph
    break."""

    def test_two_blank_lines_renders_one_paragraph_break(self):
        # 2 newlines == 1 blank line == standard Markdown paragraph break.
        from helpers._markdown import render_markdown
        md = "para one\n\npara two"
        html = render_markdown(md)
        assert "<p>para one</p>" in html
        assert "<p>para two</p>" in html

    def test_three_blank_lines_preserves_extra_space(self):
        # 4 newlines == 3 blank lines == 1 extra paragraph with <br>.
        from helpers._markdown import render_markdown
        md = "para one\n\n\n\npara two"
        html = render_markdown(md)
        assert "<p>para one</p>" in html
        assert "<p>para two</p>" in html
        assert "<br>" in html

    def test_many_blank_lines_render_proportional_breaks(self):
        from helpers._markdown import render_markdown
        md = "A\n\n\n\n\n\nB"  # 6 newlines == 5 blank lines
        html = render_markdown(md)
        # Extra blank lines produce extra <br> tags.
        assert html.count("<br>") >= 3


class TestVideoShortcodeIsolation:
    """A ``[[video …]]`` shortcode should always render as an iframe in
    embed mode, even when the user typed it directly under another line
    of text (without an intervening blank line).  The ``nl2br`` Markdown
    extension would otherwise fuse the shortcode with the previous line
    via ``<br>``, leaving the embed regex no chance to match."""

    def test_shortcode_directly_after_paragraph_renders_iframe(self):
        from helpers._markdown import render_markdown
        md = ('Intro line.\n'
              '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"]]')
        html = render_markdown(md, embed_videos=True)
        assert "<p>Intro line.</p>" in html
        assert "<iframe" in html
        # Shortcode literal should not leak into the rendered output.
        assert "[[video" not in html

    def test_shortcode_between_paragraphs_renders_iframe(self):
        from helpers._markdown import render_markdown
        md = ('Before\n'
              '[[video url="https://youtu.be/dQw4w9WgXcQ"]]\n'
              'After')
        html = render_markdown(md, embed_videos=True)
        assert "<p>Before</p>" in html
        assert "<iframe" in html
        assert "<p>After</p>" in html

    def test_shortcode_inside_fenced_code_is_not_isolated(self):
        from helpers._markdown import render_markdown
        md = '```\n[[video url="x"]]\n```'
        html = render_markdown(md, embed_videos=True)
        # Shortcode stays verbatim inside the code block (HTML-escaped quotes).
        assert "<pre>" in html
        assert "[[video" in html
        assert "<iframe" not in html
