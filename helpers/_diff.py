"""Diff computation helpers for wiki page revisions."""

import re
import difflib

from ._markdown import render_markdown


def compute_char_diff(old_text, new_text):
    """Return (added_chars, deleted_chars) between two text versions."""
    old_text = old_text or ""
    new_text = new_text or ""
    added = deleted = 0
    matcher = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            deleted += i2 - i1
        elif tag == "replace":
            added += j2 - j1
            deleted += i2 - i1
    return added, deleted


def compute_diff_html(old_text, new_text):
    """Return inline word-level diff HTML showing the full new content with change highlights.

    Output is wrapped in a <pre> block (markdown/raw view).
    """
    from markupsafe import Markup, escape

    old_text = old_text or ""
    new_text = new_text or ""

    # Tokenize preserving whitespace as separate tokens so spacing is retained
    def tokenize(text):
        """Split *text* into word and whitespace tokens for diff matching."""
        return re.split(r"(\s+)", text)

    old_tokens = tokenize(old_text)
    new_tokens = tokenize(new_text)

    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    parts = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for tok in new_tokens[j1:j2]:
                parts.append(str(escape(tok)))
        elif tag == "insert":
            for tok in new_tokens[j1:j2]:
                if tok.strip():
                    parts.append(
                        '<ins class="diff-ins">'
                        + str(escape(tok))
                        + "</ins>"
                    )
                else:
                    parts.append(str(escape(tok)))
        elif tag == "delete":
            for tok in old_tokens[i1:i2]:
                if tok.strip():
                    parts.append(
                        '<del class="diff-del">'
                        + str(escape(tok))
                        + "</del>"
                    )
                else:
                    parts.append(str(escape(tok)))
        elif tag == "replace":
            has_del = False
            for tok in old_tokens[i1:i2]:
                if tok.strip():
                    parts.append(
                        '<del class="diff-del">'
                        + str(escape(tok))
                        + "</del>"
                    )
                    has_del = True
                else:
                    parts.append(str(escape(tok)))
            sep_needed = has_del
            for tok in new_tokens[j1:j2]:
                if tok.strip():
                    if sep_needed:
                        parts.append(" ")
                        sep_needed = False
                    parts.append(
                        '<ins class="diff-ins">'
                        + str(escape(tok))
                        + "</ins>"
                    )
                else:
                    parts.append(str(escape(tok)))
    return Markup(
        '<pre class="diff-pre">'
        + "".join(parts)
        + "</pre>"
    )


def compute_formatted_diff_html(old_text, new_text):
    """Return formatted diff HTML rendered as rich HTML (formatted/rendered view).

    Computes a word-level diff on the rendered HTML of both versions, wrapping
    changed text words in <ins class="diff-ins"> and <del class="diff-del"> spans.
    HTML tags themselves are always emitted as-is (from the new version) so the
    document structure is preserved.

    Text tokens come directly from bleach-sanitized render_markdown() output so
    they are already HTML-safe; no further escaping is applied.
    """
    from markupsafe import Markup

    old_html = render_markdown(old_text or "")
    new_html = render_markdown(new_text or "")

    # Split rendered HTML into alternating text-chunks and tag-chunks.
    # Pattern: (<[^>]+>) captures tags; the rest are text fragments.
    # Text fragments from render_markdown() are already bleach-sanitized HTML,
    # so they do not need additional escaping.
    def tokenize_html(html):
        """Split rendered HTML into alternating text chunks and tag chunks."""
        return re.split(r"(<[^>]+>)", html)

    old_tokens = tokenize_html(old_html)
    new_tokens = tokenize_html(new_html)

    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    parts = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.extend(new_tokens[j1:j2])
        elif tag == "insert":
            for tok in new_tokens[j1:j2]:
                if tok.startswith("<"):
                    parts.append(tok)
                elif tok.strip():
                    parts.append('<ins class="diff-ins">' + tok + "</ins>")
                else:
                    parts.append(tok)
        elif tag == "delete":
            for tok in old_tokens[i1:i2]:
                # Emit deleted text only; skip structural tags from old version
                if not tok.startswith("<") and tok.strip():
                    parts.append('<del class="diff-del">' + tok + "</del>")
                elif not tok.startswith("<"):
                    parts.append(tok)
        elif tag == "replace":
            for tok in old_tokens[i1:i2]:
                if not tok.startswith("<") and tok.strip():
                    parts.append('<del class="diff-del">' + tok + "</del>")
                elif not tok.startswith("<"):
                    parts.append(tok)
            for tok in new_tokens[j1:j2]:
                if tok.startswith("<"):
                    parts.append(tok)
                elif tok.strip():
                    parts.append('<ins class="diff-ins">' + tok + "</ins>")
                else:
                    parts.append(tok)
    return Markup("".join(parts))
