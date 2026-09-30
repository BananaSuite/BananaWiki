"""Differences between two versions of a page (history and edit conflicts)."""

from __future__ import annotations

import difflib
import re

from markupsafe import Markup, escape

from ... import markdown

# Beyond this many tokens a word-level comparison gets slow; compare lines instead.
MAX_WORD_TOKENS = 40_000
_WORDS = re.compile(r"(\s+)")
_TAGS = re.compile(r"(<[^>]+>)")


def _tokens(text: str) -> list[str]:
    tokens = _WORDS.split(text)
    if len(tokens) > MAX_WORD_TOKENS:
        return text.splitlines(keepends=True)
    return tokens


def source_diff(old: str | None, new: str | None) -> Markup:
    """The new Markdown source with insertions and deletions marked (escaped HTML)."""
    old_tokens, new_tokens = _tokens(old or ""), _tokens(new or "")
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    parts: list[str] = []

    def mark(tokens: list[str], tag: str) -> None:
        for token in tokens:
            parts.append(f"<{tag}>{escape(token)}</{tag}>" if token.strip() else str(escape(token)))

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            parts.extend(str(escape(token)) for token in new_tokens[j1:j2])
            continue
        if op in ("delete", "replace"):
            mark(old_tokens[i1:i2], "del")
        if op in ("insert", "replace"):
            mark(new_tokens[j1:j2], "ins")
    return Markup("".join(parts))


def _html_tokens(html: str) -> list[str]:
    """Tags and single words (with their whitespace) of rendered HTML."""
    tokens: list[str] = []
    for piece in _TAGS.split(html):
        if piece.startswith("<"):
            tokens.append(piece)
        elif piece:
            tokens.extend(part for part in _WORDS.split(piece) if part)
    return tokens if len(tokens) <= MAX_WORD_TOKENS else _TAGS.split(html)


def rendered_diff(old_html: str, new_html: str) -> Markup:
    """Rendered HTML of the new version with changed text marked.

    Structure comes from the new version; deleted text from the old one is
    shown struck through. The result is sanitised again, because splicing
    fragments of two documents could otherwise produce unexpected markup.
    """
    old_tokens, new_tokens = _html_tokens(old_html or ""), _html_tokens(new_html or "")
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    parts: list[str] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            parts.extend(new_tokens[j1:j2])
            continue
        if op in ("delete", "replace"):
            for token in old_tokens[i1:i2]:
                if not token.startswith("<"):
                    parts.append(f"<del>{token}</del>" if token.strip() else token)
        if op in ("insert", "replace"):
            for token in new_tokens[j1:j2]:
                if token.startswith("<") or not token.strip():
                    parts.append(token)
                else:
                    parts.append(f"<ins>{token}</ins>")
    return Markup(markdown.sanitize("".join(parts)))


def change_counts(old: str | None, new: str | None) -> tuple[int, int]:
    """(added, removed) line counts between two versions."""
    added = removed = 0
    for line in difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed
