"""Markdown to safe HTML.

The dialect is the one BananaWiki has always stored: Python-Markdown with
``tables``, ``fenced_code``, ``codehilite``, ``toc`` and ``nl2br``, plus

* forgiving list handling (2-space nesting, lists straight after a paragraph),
* extra blank lines preserved as vertical space,
* ``[[video url="…" …]]``, ``[[canvas slug="…"]]`` and ``[[kanban board="…"]]``
  shortcodes on a line of their own,
* bare YouTube/Vimeo links on their own line embedded as players,
* ``@username`` mentions linked to profiles.

Output is sanitised with nh3 (the ammonia HTML sanitiser) against a strict
allow-list: only known classes, ids only on headings (never one of
:data:`RESERVED_IDS`), ``style`` limited to a few spacing properties with
bounded, non-negative values (nothing that could size a box to the viewport
or pull it over the page around it), links forced to
``rel="noopener noreferrer"``. Embeds, mentions and task lists are written
*after* sanitising, from validated parameters only, and never inside an
attribute value (see :func:`_escape_attribute_brackets`).

Rendering work is bounded by the source size: only the first ``[TOC]`` marker
becomes a table of contents, a document embeds at most :data:`MAX_EMBEDS`
players and boards, highlights at most :data:`MAX_HIGHLIGHT_DOCUMENT_CHARS`
of code (later blocks stay plain code), and a document whose HTML would
exceed :data:`MAX_HTML_CHARS` (or :data:`MAX_HTML_BYTES` encoded) is shown as
escaped source like any other over-budget input, so that every rendering fits
in the cache. Highlighting is also bounded in time, whatever the lexer
(see :func:`_highlight`): code is lexed in pieces of at most
:data:`HIGHLIGHT_CHUNK_CHARS`, and a document spends at most
:data:`MAX_HIGHLIGHT_SECONDS` of processor time on it. A page made of several
blocks (the page builder) shares one such allowance across them
(:func:`shared_highlighting`); answers made of many documents (a canvas)
share the time (:func:`bounded_highlighting`). Renderings of larger sources,
and of any source or snippet that needed highlighting, are kept per process
by the hash of the source (:data:`RENDER_CACHE_LIMIT`), so a page read again
is not rendered again; a rendering being made is waited for, not made again
by every concurrent reader.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from html import escape
from urllib.parse import quote
from xml.etree.ElementTree import Element

import markdown as _markdown
import nh3
from markdown.extensions import Extension
from markdown.extensions.attr_list import get_attrs_and_remainder
from markdown.extensions.codehilite import CodeHilite, CodeHiliteExtension, HiliteTreeprocessor
from markdown.extensions.fenced_code import FencedBlockPreprocessor, FencedCodeExtension
from markdown.extensions.toc import TocExtension, TocTreeprocessor
from markdown.extensions.toc import slugify as _toc_slugify
from markdown.inlinepatterns import SimpleTagInlineProcessor
from markdown.treeprocessors import Treeprocessor
from pygments.formatters import HtmlFormatter
from pygments.lexer import Lexer
from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
from pygments.token import STANDARD_TYPES
from pygments.util import ClassNotFound

from bananawiki.core.numbers import ascii_int
from bananawiki.wiki import highlighter

MAX_SOURCE_CHARS = 1_000_000
MAX_PARSE_WORK = 32_000_000
MAX_MARKUP_DEPTH = 64
MAX_HEADINGS = 1000
MAX_HIGHLIGHT_LINES = 256
MAX_LEXER_SAMPLE_CHARS = 4096
MAX_HIGHLIGHT_CODE_CHARS = 64 * 1024
# Pygments needs up to about 14 µs per character of token-dense code: one
# document highlights at most this much, each block also paying for its lexer
# and formatter. Later blocks stay plain code.
MAX_HIGHLIGHT_DOCUMENT_CHARS = 64 * 1024
HIGHLIGHT_BLOCK_COST = 256
# Many Pygments lexers need time growing with the square (or more) of a run of
# similar characters, and some produce no token before they are done: one
# token of a 64 KB block can take minutes. Code is lexed in pieces of at most
# this many characters, cut between lines (a block with a longer line is shown
# without colours), so that no lexer sees more at once: every lexer of
# Pygments, on 1 KB of the inputs scripts/measure_lexers.py tries, needs at
# most about 0.06 s for a token, except those in _UNBOUNDED_LEXERS...
HIGHLIGHT_CHUNK_CHARS = 1024
# ...and a document (or an answer of many, see bounded_highlighting) spends
# at most this much processor time highlighting: the block being highlighted
# when it runs out, and every later one, stay plain code.
MAX_HIGHLIGHT_SECONDS = 0.5
# ...which need seconds for a few dozen characters (exponential or cubic time): code in their
# languages is shown without colours. Measured with this version of Pygments (see
# scripts/measure_lexers.py; tests check that the installed version is the one measured).
MEASURED_PYGMENTS_VERSION = "2.21.0"
_UNBOUNDED_LEXERS = frozenset({"EasytrieveLexer", "MapleLexer"})
# Python-Markdown handles blank-line separated blocks one at a time, at a cost
# that grows faster than their number; a highlighted code block costs about
# ten paragraphs, a table four. Ordinary megabyte articles stay near 12,000.
MAX_BLOCK_COST = 25_000
_CODE_BLOCK_COST = 10
_TABLE_BLOCK_COST = 4
# Highlighted code reaches about eleven times its source. Reference links can
# repeat one long URL in every use, which is what this limit stops.
MAX_HTML_CHARS = 16_000_000
# The finished HTML is bounded in UTF-8 bytes too (as the render cache keeps it), so that
# every rendering fits in one cache entry: one emoji would otherwise make Python hold a
# 6 M-character page in 24 MB and the page would be rendered again on every view.
MAX_HTML_BYTES = 16_000_000
MAX_EMBEDS = 200
# Player detection rescans the rest of a URL from each "youtube.com/watch?" in it.
MAX_VIDEO_URL_CHARS = 2048
# Room for the longest video URL and the other options, as escaped HTML.
MAX_SHORTCODE_CHARS = MAX_VIDEO_URL_CHARS + 512
# Excerpts render only the start of a page, under smaller allowances: page
# lists show many of them in one view.
EXCERPT_SOURCE_CHARS = 4096
MAX_EXCERPT_WORK = 1_000_000
MAX_EXCERPT_BLOCK_COST = 400
# The toc extension's search for a free heading id grows with every repeated
# title, so heading counts are weighed too.
MAX_EXCERPT_HEADINGS = 200
#: Bytes of rendered HTML kept per process (UTF-8 encoded); one entry takes at most half.
RENDER_CACHE_LIMIT = 32 * 1024 * 1024
#: Shorter sources render faster than their cache entry is worth keeping.
RENDER_CACHE_MIN_CHARS = 2048

_TAGS = {
    "a", "abbr", "acronym", "b", "blockquote", "br", "code", "dd", "del", "details", "div", "dl", "dt",
    "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd",
    "li", "mark", "ol", "p", "pre", "s", "section", "aside", "small", "span", "strong", "sub", "summary",
    "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height", "style"},
    "abbr": {"title"},
    "acronym": {"title"},
    "figure": {"style"},
    "div": {"style"},
    "td": {"align", "colspan", "rowspan"},
    "th": {"align", "colspan", "rowspan"},
    "ol": {"start"},
    "h1": {"id"}, "h2": {"id"}, "h3": {"id"}, "h4": {"id"}, "h5": {"id"}, "h6": {"id"},
}
_TAG_ATTRIBUTE_VALUES = {
    "a": {"target": {"_blank"}},
    "img": {"loading": {"lazy", "eager"}},
}
_STYLE_PROPERTIES = {
    "margin-top", "margin-bottom", "margin-left", "margin-right", "padding",
    "width", "max-width", "height",
    "--media-margin-top", "--media-margin-bottom", "--media-padding", "--media-max-width",
    "--media-aspect-ratio",
}
_PYGMENTS_CLASSES = {cls for cls in STANDARD_TYPES.values() if cls}
_CONTENT_CLASSES = {
    # Classes the editor toolbar inserts for media layout; styled in content.css.
    "media-left", "media-right", "media-center", "media-full", "media-inline",
    "media-image-container", "media-caption", "table-wrapper", "toc",
}
_ALLOWED_CLASSES = {
    "div": {"codehilite", "toc", *_CONTENT_CLASSES},
    "span": _PYGMENTS_CLASSES | {"hll", "linenos", "normal", "special"},
    "pre": {"codehilite", "code-block"},
    "code": set(),
    "img": _CONTENT_CLASSES,
    "figure": _CONTENT_CLASSES,
    "figcaption": _CONTENT_CLASSES,
    "table": {"table-wrapper"},
    "p": _CONTENT_CLASSES,
}
# Values of those properties: non-negative lengths that cannot size a box to
# the viewport or move it over the page around it (no vw/vh, calc() or var()).
_LENGTH = r"(?:0|\d{1,4}(?:\.\d{1,3})?(?:px|%|em|rem))"
_STYLE_VALUES = {
    "length": re.compile(rf"{_LENGTH}|auto"),
    "lengths": re.compile(rf"{_LENGTH}(?: {_LENGTH}){{0,3}}"),
    "max": re.compile(rf"{_LENGTH}|none"),
    "ratio": re.compile(r"\d{1,4}(?:\.\d{1,3})?(?: ?/ ?\d{1,4}(?:\.\d{1,3})?)?|auto"),
}
_STYLE_VALUE_KIND = {
    "margin-top": "length", "margin-bottom": "length", "margin-left": "length", "margin-right": "length",
    "padding": "lengths", "width": "length", "max-width": "max", "height": "length",
    "--media-margin-top": "length", "--media-margin-bottom": "length", "--media-padding": "lengths",
    "--media-max-width": "max", "--media-aspect-ratio": "ratio",
}
# Pixel lengths above this would only push content over the page around it.
_MAX_STYLE_PX = 4000
_MAX_STYLE_PERCENT = 100
_URL_SCHEMES = {"http", "https", "mailto", "ftp"}
_HEADING_ID = re.compile(r"^[A-Za-z0-9_\-.:]{1,120}$")
#: Ids the interface links to on every page and on the page view: the skip link (``#main``), the
#: menu button (``aria-controls="sidebar"``) and the attachments panel, where an upload returns
#: (``#attachments``). Headings come after the first two and before the panel, so a heading with
#: one of them would be skipped by its own table of contents or catch the interface's link: it gets
#: ``_1`` added instead, as a second heading with the same text does.
RESERVED_IDS = frozenset({"main", "sidebar", "attachments"})

_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM = re.compile(r"^(\d+[.)]|[-*+])\s+\S")
_INDENTED_LIST_ITEM = re.compile(r"^([ \t]*)(\d+[.)]|[-*+])\s")
# A single ``\s``: the attribute class accepts further whitespace, and ``\s+``
# here would make an unclosed shortcode quadratic in its line length.
_SHORTCODE_LINE = re.compile(r"^[ \t]*\[\[(?:video|canvas|kanban)\s[^\]]*?\]\][ \t]*$", re.IGNORECASE)
_BLANK_RUN = re.compile(r"(\n[ \t]*){3,}")
_FENCE_RUN = re.compile(r"(`{3,}|~{3,})[ ]*")
# An opening fence whose legacy ``hl_lines`` value may continue on later lines.
_LEGACY_HL_OPENING = re.compile(r"(`{3,}|~{3,})[ ]*\.?[\w#.+-]*[ ]*hl_lines=([\"'])")


def _split_fences(text: str) -> list[tuple[bool, list[str]]]:
    """Split source lines into runs of (inside_fence, lines).

    A loose reading that keeps preprocessing out of code, indented fences
    included. What the parser really treats as code is :func:`_fenced_code`.
    """
    runs: list[tuple[bool, list[str]]] = []
    inside = False
    current: list[str] = []
    for line in text.split("\n"):
        if _FENCE.match(line):
            if inside:
                current.append(line)
                runs.append((True, current))
                current = []
                inside = False
                continue
            if current:
                runs.append((False, current))
            current = [line]
            inside = True
            continue
        current.append(line)
    if current:
        runs.append((inside, current))
    return runs


def _normalise_whitespace(text: str) -> str:
    """*text* as Python-Markdown's first preprocessor hands it on."""
    text = text.replace("\x02", "").replace("\x03", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n") + "\n\n"
    return re.sub(r"(?<=\n) +\n", "\n", text.expandtabs(4))


def _lines_ending_in_quotes(lines: list[str]) -> dict[str, list[int]]:
    """The numbers of the *lines* that end in each quote character, trailing spaces aside."""
    ends: dict[str, list[int]] = {}
    for number, line in enumerate(lines):
        line = line.rstrip(" ")
        if line.endswith(('"', "'")):
            ends.setdefault(line[-1], []).append(number)
    return ends


def _fenced_code(lines: list[str]) -> tuple[list[tuple[int, int]], int, str | None]:
    """The code blocks Python-Markdown's fenced_code preprocessor extracts from normalised *lines*.

    It searches for an opening fence at the start of a line and closes it at
    the first later line holding exactly the same fence. An opening without
    a closer stays text; one with malformed ``{attributes}`` is skipped, and
    the lines after it are searched again. Returns the first and last line of
    each block, the characters the preprocessor's pattern scans in vain (an
    opening without a closer rescans the rest of the document, once for each
    line its legacy hl_lines value may end on) and the fence of the first
    opening left without a closer that a closer at the end would turn into
    a block.
    """
    closers: dict[str, list[int]] = {}
    starts: dict[int, int] = {}  # where each line that may be a fence starts
    size = 0
    for number, line in enumerate(lines):
        if line.startswith(("```", "~~~")):
            starts[number] = size
            if closer := _FENCE_RUN.fullmatch(line):
                closers.setdefault(closer.group(1), []).append(number)
        size += len(line) + 1
    quote_ends: dict[str, list[int]] | None = None
    blocks: list[tuple[int, int]] = []
    wasted = 0
    unclosed = None
    candidates = list(starts)
    index = 0
    while index < len(candidates):
        number = candidates[index]
        index += 1
        line = lines[number]
        fence = _FENCE_RUN.match(line)
        if fence is None:
            continue
        mark, spaces = fence.group(1), fence.end() - fence.end(1)
        # The number of spaces after the fence does not change the reading;
        # probing the line with all of them would be as slow as the parser.
        probe = mark + " " * min(spaces, 1) + line[fence.end():]
        opening = FencedBlockPreprocessor.FENCED_BLOCK_RE.fullmatch(probe + "\n" + mark)
        # The pattern retries an opening that never closes for each way of
        # sharing those spaces between its optional parts, up to 2 * (spaces + 2)
        # scans of the rest; trying the ways alone is quadratic in the spaces.
        rescan = (size - starts[number]) * 2 * (spaces + 2)
        wasted_here = spaces * spaces
        end = number
        quote = opening.group("quot") if opening else None
        if opening is None:
            legacy = _LEGACY_HL_OPENING.match(probe)
            if legacy is None:
                wasted += wasted_here
                continue
            quote = legacy.group(2)
        tries = 1
        if quote:
            # A legacy hl_lines value may run on to any later line that ends
            # in its quote, and the rest is searched again from each of them.
            if quote_ends is None:
                quote_ends = _lines_ending_in_quotes(lines)
            ends = quote_ends.get(quote, [])
            position = bisect_right(ends, number)
            # The opening line counts when the value can already end there.
            tries = len(ends) - position + (opening is not None)
            if opening is None:
                # Otherwise the value ends on the first of those lines.
                if position == len(ends):
                    wasted += wasted_here + rescan
                    continue
                end = ends[position]
        wasted_here += rescan * tries
        attrs = opening.group("attrs") if opening else None
        malformed = bool(attrs and get_attrs_and_remainder(attrs)[1])
        closing = closers.get(mark, [])
        position = bisect_right(closing, end)
        if position == len(closing):
            wasted += wasted_here
            if not malformed:  # a closer would only make the parser skip it
                unclosed = unclosed or mark
            continue
        last = closing[position]
        if malformed:
            wasted += starts[last] + len(lines[last]) + 1 - starts[number]
            continue
        blocks.append((number, last))
        index = bisect_right(candidates, last)
    return blocks, wasted, unclosed


def _map_prose(text: str, transform: Callable[[list[str]], list[str]]) -> str:
    out: list[str] = []
    for inside, lines in _split_fences(text):
        out.extend(lines if inside else transform(lines))
    return "\n".join(out)


def _blank_line_before_lists(lines: list[str]) -> list[str]:
    out: list[str] = []
    for index, line in enumerate(lines):
        if index and _LIST_ITEM.match(line):
            previous = lines[index - 1]
            if previous.strip() and not _LIST_ITEM.match(previous) and not previous.startswith((" ", "\t")):
                out.append("")
        out.append(line)
    return out


def _normalise_list_block(block: list[str]) -> list[str]:
    indents = []
    for line in block:
        if line.strip():
            expanded = line.expandtabs(4)
            spaces = len(expanded) - len(expanded.lstrip(" "))
            if spaces:
                indents.append(spaces)
    if not indents or min(indents) >= 4:
        return block
    # Scale the smallest indent to Python-Markdown's 4 spaces and the others in proportion
    # (rounding up), so a sub-list indented under "1. " by 3 spaces (CommonMark, GitHub) nests too.
    unit = min(indents)
    result = []
    for line in block:
        if not line.strip():
            result.append(line)
            continue
        expanded = line.expandtabs(4)
        spaces = len(expanded) - len(expanded.lstrip(" "))
        result.append(" " * -(-spaces * 4 // unit) + expanded[spaces:])
    return result


def _normalise_list_indent(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _LIST_ITEM.match(line.lstrip()) and line[:1] not in (" ", "\t"):
            j = i
            saw_blank = False
            while j < len(lines):
                current = lines[j]
                if not current.strip():
                    if saw_blank:
                        break
                    saw_blank = True
                    j += 1
                    continue
                if _INDENTED_LIST_ITEM.match(current) or (current[:1] in (" ", "\t") and not saw_blank):
                    saw_blank = False
                    j += 1
                    continue
                break
            out.extend(_normalise_list_block(lines[i:j]))
            i = j
            continue
        out.append(line)
        i += 1
    return out


def _isolate_shortcodes(lines: list[str]) -> list[str]:
    out: list[str] = []
    for index, line in enumerate(lines):
        if _SHORTCODE_LINE.match(line):
            if out and out[-1].strip():
                out.append("")
            out.append(line.strip())
            if index + 1 < len(lines) and lines[index + 1].strip():
                out.append("")
            continue
        out.append(line)
    return out


def _preserve_blank_runs(lines: list[str]) -> list[str]:
    text = "\n".join(lines)
    text = _BLANK_RUN.sub(lambda m: "\n\n" + "<br>" * (m.group(0).count("\n") - 2) + "\n\n", text)
    return text.split("\n")


def _preprocess(text: str, fix_lists: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if fix_lists:
        text = _map_prose(text, _blank_line_before_lists)
    text = _map_prose(text, _normalise_list_indent)
    text = _map_prose(text, _isolate_shortcodes)
    return _map_prose(text, _preserve_blank_runs)


def _parser_work_exceeded(text: str, limit: int = MAX_PARSE_WORK, block_limit: int = MAX_BLOCK_COST,
                          heading_limit: int = MAX_HEADINGS, used: list[int] | None = None) -> bool:
    """Bound repeated delimiter searches, nesting and block count before parsing prose.

    The Markdown link parser searches the remaining paragraph for each
    opening bracket. A byte limit alone allows quadratic work on unmatched
    input. Estimate that search work per paragraph, after the parser's own
    whitespace normalisation. Fenced code is literal and bypasses inline
    parsing, so it does not consume this allowance; only the blocks the
    parser really extracts count as code, and the work of searching for
    fences that never close does. Blocks, fenced ones included, are weighed
    against *block_limit* and headings against *heading_limit*. When it is
    not exceeded, *used* (if given) receives ``[work, blocks, headings]``.
    """
    lines = _normalise_whitespace(text).split("\n")
    code, work, _ = _fenced_code(lines)
    if work > limit:
        return True
    prose: list[str] = []
    blocks = start = 0
    for first, last in code:
        prose.extend(lines[start:first])
        prose.extend(("", ""))
        blocks += _CODE_BLOCK_COST
        start = last + 1
    prose.extend(lines[start:])
    source = "\n".join(prose)
    headings = 0
    for paragraph in source.split("\n\n"):
        block = paragraph.lstrip("\n")
        if block.strip():
            # Indented code is highlighted too (or is a nested list item).
            blocks += (_CODE_BLOCK_COST if block.startswith(("    ", "\t"))
                       else _TABLE_BLOCK_COST if "|" in block else 1)
        if blocks > block_limit:
            return True
        delimiters = sum(paragraph.count(marker) for marker in "[*_`<~&\\\n")
        work += len(paragraph) * delimiters
        if work > limit:
            return True
        square_depth = round_depth = 0
        for line in paragraph.split("\n"):
            stripped = line.lstrip()
            if len(line) - len(stripped) > 4 * MAX_MARKUP_DEPTH and _LIST_ITEM.match(stripped):
                return True
            prefix_depth = 0
            while True:
                if stripped.startswith(">"):
                    stripped = stripped[1:].lstrip()
                else:
                    marker = re.match(r"^(?:[-+*]|\d+[.)])\s+", stripped)
                    if marker is None:
                        break
                    stripped = stripped[marker.end():].lstrip()
                prefix_depth += 1
                if prefix_depth > MAX_MARKUP_DEPTH:
                    return True
            if stripped.startswith("#") or re.fullmatch(r"[=-]+", stripped.rstrip()):
                headings += 1
                # Repeated heading slugs otherwise cause quadratic collision
                # searches across paragraphs in the toc extension.
                if headings > heading_limit:
                    return True
        # Skip escaped characters and code spans; brackets inside literal
        # code do not create a nested Markdown document.
        escaped = False
        code_ticks = 0
        index = 0
        while index < len(paragraph):
            char = paragraph[index]
            index += 1
            if escaped:
                escaped = False
                continue
            if char == "\\" and not code_ticks:
                escaped = True
                continue
            if char == "`":
                ticks = 1
                while index < len(paragraph) and paragraph[index] == "`":
                    ticks += 1
                    index += 1
                if code_ticks == ticks:
                    code_ticks = 0
                elif not code_ticks:
                    code_ticks = ticks
                continue
            if code_ticks:
                continue
            if char == "[":
                square_depth += 1
            elif char == "]":
                square_depth = max(0, square_depth - 1)
            elif char == "(":
                round_depth += 1
            elif char == ")":
                round_depth = max(0, round_depth - 1)
            if max(square_depth, round_depth) > MAX_MARKUP_DEPTH:
                return True
    if used is not None:
        used[:] = [work, blocks, headings]
    return False


def _plain_source(text: str) -> str:
    """A safe, non-recursive fallback that preserves every source character."""
    return "<pre>" + escape(text) + "</pre>"


def plain_html(text: str | None) -> str:
    """*text* shown as it was typed, escaped, without rendering it (what :func:`render` falls back to)."""
    return _plain_source(text or "")


def _style_value_allowed(kind: str, value: str) -> bool:
    if not _STYLE_VALUES[kind].fullmatch(value):
        return False
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)(px|%|em|rem)?", value):
        if kind == "ratio" or not unit:
            continue
        ceiling = _MAX_STYLE_PX if unit == "px" else _MAX_STYLE_PERCENT if unit == "%" else _MAX_STYLE_PX / 16
        if float(number) > ceiling:
            return False
    return True


def filter_style(value: str) -> str | None:
    """The declarations of a ``style`` attribute with an allowed property and value, normalised."""
    kept = []
    for declaration in value.split(";")[:32]:
        name, colon, raw = declaration.partition(":")
        name = name.strip().lower()
        raw = " ".join(raw.split()).lower()
        kind = _STYLE_VALUE_KIND.get(name)
        if colon and kind and _style_value_allowed(kind, raw):
            kept.append(f"{name}:{raw}")
    return ";".join(kept) or None


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    if attribute == "id":
        if not _HEADING_ID.match(value):
            return None
        return value + "_1" if value in RESERVED_IDS else value
    if attribute == "style":
        return filter_style(value)
    if tag == "img" and attribute in {"width", "height"}:
        return value if re.fullmatch(r"\d{1,4}%?", value) else None
    return value


def sanitize(html: str) -> str:
    """Sanitise HTML produced from user Markdown."""
    return nh3.clean(
        html,
        tags=_TAGS,
        clean_content_tags={"script", "style"},
        attributes=_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        tag_attribute_values=_TAG_ATTRIBUTE_VALUES,
        allowed_classes=_ALLOWED_CLASSES,
        filter_style_properties=_STYLE_PROPERTIES,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


# Post-sanitisation embeds

# The sanitiser writes every attribute value in double quotes with ``"`` and
# ``&`` escaped, and ``<`` and ``>`` escaped in text, but only nh3 0.3.7 and
# later also escape ``<`` and ``>`` in attribute values. The rewrites below
# find tags with patterns, so the brackets are escaped here as well: a
# pattern then can never match, and write markup into, an attribute value.
_SERIALISED_TAG = re.compile(r'<[^<>"]*(?:"[^"]*"[^<>"]*)*>')
_QUOTED_VALUE = re.compile(r'"[^"]*"')


def _escape_attribute_brackets(html: str) -> str:
    def tag(match: re.Match[str]) -> str:
        return _QUOTED_VALUE.sub(lambda value: value.group(0).replace("<", "&lt;").replace(">", "&gt;"),
                                 match.group(0))

    return _SERIALISED_TAG.sub(tag, html) if '"' in html else html


# Attributes stay inside their own paragraph (inline tags allowed) and are
# bounded, so an unclosed ``[[video`` cannot rescan the rest of the document.
_SHORTCODE_ATTRS = rf"(?P<attrs>(?:[^\]<]|<(?!/?p>)){{0,{MAX_SHORTCODE_CHARS}}}?)"
_VIDEO_SHORTCODE = re.compile(rf"<p>\s*\[\[video\s{_SHORTCODE_ATTRS}\]\]\s*</p>", re.IGNORECASE)
_EMBED_SHORTCODE = re.compile(rf"<p>\s*\[\[(?P<kind>canvas|kanban)\s{_SHORTCODE_ATTRS}\]\]\s*</p>", re.IGNORECASE)
# Names start after a non-letter, so a long word is not retried at every position.
# Values may hold escaped ampersands (``&amp;`` in a URL query) up to the closing quote.
_SHORTCODE_ATTR = re.compile(r'(?<![a-z])([a-z]+)\s*=\s*(?:"|&quot;)((?:[^"&]|&(?!quot;))*)(?:"|&quot;)',
                             re.IGNORECASE)
_BARE_VIDEO = re.compile(
    r'<p>\s*(?:<a href="(?P<href>https?://[^"]+)"[^>]*>(?P=href)</a>|(?P<bare>https?://[^\s<>"]+))\s*</p>',
    re.IGNORECASE,
)
_RATIOS = {"16:9": "ratio-16x9", "4:3": "ratio-4x3", "1:1": "ratio-1x1"}
_DIMENSION = re.compile(r"^\d{2,4}$")


def video_embed_src(url: str | None) -> str | None:
    """Canonical player URL for YouTube or Vimeo links, else None."""
    if not url or len(url) > MAX_VIDEO_URL_CHARS:
        return None
    match = re.search(r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if match and re.match(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/", url, re.IGNORECASE):
        return f"https://www.youtube-nocookie.com/embed/{match.group(1)}"
    match = re.match(r"https?://(?:www\.|player\.)?vimeo\.com/(?:video/)?(\d+)", url, re.IGNORECASE)
    if match:
        return f"https://player.vimeo.com/video/{match.group(1)}"
    return None


def _flag(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() == "true"


def video_iframe(source_url: str, **options: str | None) -> str | None:
    """Build the player markup for a supported video URL (options validated here)."""
    src = video_embed_src(source_url)
    if not src:
        return None
    width = options.get("width") or ""
    width = width if _DIMENSION.match(width) else ""
    align = (options.get("align") or "center").lower()
    align = align if align in {"none", "left", "right", "center"} else "center"
    ratio = options.get("ratio") or "16:9"
    ratio = ratio if ratio in _RATIOS else "16:9"
    margin = options.get("margin") or ""
    margin = margin if ascii_int(margin, 3) is not None and int(margin) <= 200 else ""
    params = []
    if _flag(options.get("autoplay"), False):
        params.append("autoplay=1")
    if _flag(options.get("loop"), False):
        params.append("loop=1")
    if not _flag(options.get("controls"), True) and "youtube" in src:
        params.append("controls=0")
    if params:
        src += "?" + "&".join(params)
    data = [
        f'data-source-url="{escape(source_url, quote=True)}"',
        f'data-align="{align}"',
        f'data-ratio="{ratio}"',
    ]
    if width:
        data.append(f'data-width="{width}"')
    if margin:
        data.append(f'data-margin="{margin}"')
    return (
        f'<div class="bw-video bw-video-{align} {_RATIOS[ratio]}" {" ".join(data)}>'
        f'<iframe src="{escape(src, quote=True)}" title="Video" loading="lazy" '
        'allow="fullscreen; picture-in-picture" '
        'referrerpolicy="strict-origin-when-cross-origin"></iframe></div>'
    )


def _shortcode_attrs(raw: str) -> dict[str, str]:
    return {key.lower(): value for key, value in _SHORTCODE_ATTR.findall(raw)}


class _EmbedBudget:
    """Players and boards a document may still embed; later ones stay literal text."""

    def __init__(self) -> None:
        self.left = MAX_EMBEDS

    def spend(self, markup: str | None, match: re.Match[str]) -> str:
        if not markup:
            return match.group(0)
        self.left -= 1
        return markup


def _embed_videos(html: str, bare_links: bool, budget: _EmbedBudget) -> str:
    def shortcode(match: re.Match[str]) -> str:
        if not budget.left:
            return match.group(0)
        attrs = _shortcode_attrs(match.group("attrs"))
        url = attrs.pop("url", "").replace("&amp;", "&")
        return budget.spend(video_iframe(url, **attrs), match)

    html = _VIDEO_SHORTCODE.sub(shortcode, html)
    if bare_links:
        def bare(match: re.Match[str]) -> str:
            if not budget.left:
                return match.group(0)
            url = (match.group("href") or match.group("bare") or "").replace("&amp;", "&")
            return budget.spend(video_iframe(url), match)

        html = _BARE_VIDEO.sub(bare, html)
    return html


def _embed_boards(html: str, budget: _EmbedBudget) -> str:
    def replace(match: re.Match[str]) -> str:
        if not budget.left:
            return match.group(0)
        kind = match.group("kind").lower()
        attrs = _shortcode_attrs(match.group("attrs"))
        ref = attrs.get("slug" if kind == "canvas" else "board", "")
        if not ref or len(ref) > 200:
            return match.group(0)
        width = attrs.get("width", "")
        height = attrs.get("height", "")
        dims = ""
        if _DIMENSION.match(width):
            dims += f' data-width="{width}"'
        if _DIMENSION.match(height):
            dims += f' data-height="{height}"'
        return budget.spend(
            f'<div class="bw-embed bw-embed-{kind}" data-embed-type="{kind}" '
            f'data-embed-ref="{escape(ref, quote=True)}"{dims}></div>',
            match,
        )

    return _EMBED_SHORTCODE.sub(replace, html)


_TAG_SPLIT = re.compile(r"(<[^>]+>)")
_MENTION = re.compile(r"(?<![\w@/])@([A-Za-z0-9_-]{3,50})\b")
_SKIP_TAGS = {"a", "code", "pre"}


def _link_mentions(html: str, profile_url: Callable[[str], str]) -> str:
    parts = _TAG_SPLIT.split(html)
    depth = 0
    for index, part in enumerate(parts):
        if not part:
            continue
        if part.startswith("<"):
            match = re.match(r"<(/?)([a-z0-9]+)", part, re.IGNORECASE)
            if match and match.group(2).lower() in _SKIP_TAGS and not part.endswith("/>"):
                depth += -1 if match.group(1) else 1
                depth = max(depth, 0)
            continue
        if depth == 0 and "@" in part:
            parts[index] = _MENTION.sub(
                lambda m: f'<a class="mention" href="{escape(profile_url(m.group(1)), quote=True)}">@{m.group(1)}</a>',
                part,
            )
    return "".join(parts)


def _default_profile_url(username: str) -> str:
    return "/users/" + quote(username)


class _Strikethrough(Extension):
    """``~~text~~`` becomes ``<del>text</del>`` (the editor toolbar inserts it)."""

    def extendMarkdown(self, md):  # noqa: N802 - Python-Markdown API name
        md.inlinePatterns.register(SimpleTagInlineProcessor(r"(~~)(.+?)~~", "del"), "bw_del", 175)


class _OutOfTime(Exception):
    """Highlighting used up its :class:`_HighlightClock`."""


class _HighlightClock:
    """Processor time a document (or an answer) may still spend highlighting (:data:`MAX_HIGHLIGHT_SECONDS`)."""

    def __init__(self) -> None:
        self.left = MAX_HIGHLIGHT_SECONDS
        self.cut = 0  # blocks left plain for want of time
        self.busy = False  # some block left plain because no highlighting helper was free: not worth keeping

    def expired(self) -> bool:
        """Whether no time is left (counted as a block cut short)."""
        if self.left > 0:
            return False
        self.cut += 1
        return True

    def tokens(self, tokens: Iterable[tuple[object, str]]) -> Iterator[tuple[object, str]]:
        """*tokens* until the time is up, then :class:`_OutOfTime`.

        Processor time of this thread: waiting for other threads costs nothing.
        """
        start = time.thread_time()
        deadline = start + self.left
        try:
            for token in tokens:
                if time.thread_time() > deadline:
                    self.left = 0
                    self.cut += 1
                    raise _OutOfTime
                yield token
        finally:
            self.left = max(0.0, self.left - (time.thread_time() - start))


class _HighlightBudget:
    """Characters of code one document may still highlight (see :data:`MAX_HIGHLIGHT_DOCUMENT_CHARS`),
    and its :class:`_HighlightClock`."""

    def __init__(self, allowance: int = MAX_HIGHLIGHT_DOCUMENT_CHARS, clock: _HighlightClock | None = None) -> None:
        self.left = allowance
        self.refused = 0  # blocks left plain
        self.clock = clock or _shared_clock.get() or _HighlightClock()

    def take(self, code: str) -> bool:
        """Whether *code* may be highlighted; once a block does not fit, no later one is."""
        cost = len(code) + HIGHLIGHT_BLOCK_COST
        if cost > self.left or self.clock.expired():
            self.cut()
            return False
        self.left -= cost
        return True

    def cut(self) -> None:
        """A block stays plain code, and so does every later one."""
        self.left = 0
        self.refused += 1


# The allowance of the page being rendered when it is made of several blocks.
_shared_budget: ContextVar[_HighlightBudget | None] = ContextVar("bananawiki_highlight_budget", default=None)
# The time of the answer being made when it renders several documents.
_shared_clock: ContextVar[_HighlightClock | None] = ContextVar("bananawiki_highlight_clock", default=None)


@contextmanager
def bounded_highlighting() -> Iterator[None]:
    """Let every rendering inside share one highlighting time (:data:`MAX_HIGHLIGHT_SECONDS`).

    For answers that render many documents, each with its own character
    allowance (the nodes of a canvas). Renderings cut short for want of time
    are not kept in the cache (see :func:`highlighting_cut_short`).
    """
    if _shared_clock.get() is not None:
        yield
        return
    token = _shared_clock.set(_HighlightClock())
    try:
        yield
    finally:
        _shared_clock.reset(token)


def highlighting_cut_short() -> int:
    """How many blocks the time shared by :func:`bounded_highlighting` left plain so far (0 outside it)."""
    clock = _shared_clock.get()
    return clock.cut if clock is not None else 0


@contextmanager
def shared_highlighting() -> Iterator[None]:
    """Let every :func:`render` and :func:`highlight_code` inside share one highlighting allowance.

    For a page made of many blocks (the page builder), which would otherwise
    get a whole allowance for each block. Results cut short by the shared
    allowance are not kept in the cache.
    """
    clock = _shared_clock.get() or _HighlightClock()
    tokens = _shared_clock.set(clock), _shared_budget.set(_HighlightBudget(clock=clock))
    try:
        yield
    finally:
        _shared_budget.reset(tokens[1])
        _shared_clock.reset(tokens[0])


class _DocumentLimits:
    """What the documents rendered as one (:func:`one_document`) may still use of one document's limits.

    Parse work, blocks and headings (:func:`_parser_work_exceeded`), HTML
    (:data:`MAX_HTML_CHARS`) and embeds (:data:`MAX_EMBEDS`): a document that
    does not fit in what is left is shown as escaped source, and highlighted
    code that does not fit as plain code. *reserve* bytes of the HTML are kept
    for those fallbacks, so that all the HTML together stays within one
    document's (:func:`render_parts` reserves what all of them would take).
    """

    def __init__(self, reserve: int = 0) -> None:
        self.work = MAX_PARSE_WORK
        self.blocks = MAX_BLOCK_COST
        self.headings = MAX_HEADINGS
        self.html = max(0, min(MAX_HTML_CHARS, MAX_HTML_BYTES) - reserve)
        self.embeds = _EmbedBudget()

    def exceeded(self, text: str, used: list[int]) -> bool:
        """Whether *text* needs more than is left; else what it needs, the most of each, goes into *used*."""
        measured: list[int] = []
        if _parser_work_exceeded(text, self.work, self.blocks, self.headings, measured):
            return True
        used[:] = [max(pair) for pair in zip(used, measured, strict=True)] if used else measured
        return False

    def take(self, used: list[int]) -> None:
        work, blocks, headings = used
        self.work, self.blocks, self.headings = self.work - work, self.blocks - blocks, self.headings - headings

    def fits(self, html: str) -> bool:
        """Whether *html* fits in the HTML left; if it does, it is taken from it."""
        size = _utf8_size(html)
        if size > self.html:
            return False
        self.html -= size
        return True


# The limits of the documents being rendered as one (the blocks of a builder page).
_shared_limits: ContextVar[_DocumentLimits | None] = ContextVar("bananawiki_document_limits", default=None)


@contextmanager
def one_document(reserve: int = 0) -> Iterator[None]:
    """Let every :func:`render` inside count against the limits of one document, and share its highlighting.

    For a page made of many Markdown blocks (the page builder): without it each
    block would get the whole parse, heading, output and embed limits of a
    document (and :func:`shared_highlighting` only shares the highlighting).
    Renderings inside are not kept by themselves, as they depend on the
    blocks before them: :func:`render_parts` keeps the whole. *reserve* is
    the HTML kept for blocks shown as source (:class:`_DocumentLimits`).
    """
    with shared_highlighting():
        token = _shared_limits.set(_DocumentLimits(reserve))
        try:
            yield
        finally:
            _shared_limits.reset(token)


# Where a piece of code preferably ends, best first, and how far into the piece: before a line
# starting in the first column (after a blank line, at best), which in most languages starts a
# new definition rather than continuing a string or comment; else after a blank line or any line.
_CHUNK_BREAKS = ((re.compile(r"\n\n+(?=\S)"), 4), (re.compile(r"\n(?=\S)"), 4), (re.compile(r"\n\n"), 2),
                 (re.compile(r"\n"), HIGHLIGHT_CHUNK_CHARS))


def _code_chunks(text: str) -> list[str] | None:
    """*text* cut between lines into pieces of at most :data:`HIGHLIGHT_CHUNK_CHARS` (see
    :data:`_CHUNK_BREAKS`); None when a line is longer than that."""
    chunks = []
    start = 0
    while len(text) - start > HIGHLIGHT_CHUNK_CHARS:
        limit = start + HIGHLIGHT_CHUNK_CHARS
        cut = None
        for brk, part in _CHUNK_BREAKS:
            for found in brk.finditer(text, start + HIGHLIGHT_CHUNK_CHARS // part, limit):
                cut = found.end()
            if cut is not None:
                break
        if cut is None:
            return None
        chunks.append(text[start:cut])
        start = cut
    chunks.append(text[start:])
    return chunks


def _highlight(code: str, lexer: Lexer, formatter: HtmlFormatter, clock: _HighlightClock) -> str | None:
    """``pygments.highlight(code, lexer, formatter)`` within *clock*; None when it ran out (plain code).

    Lexers run in a helper process (:mod:`.highlighter`) that is stopped when
    the clock runs out: some need exponential time inside one token, which
    nothing in this process could interrupt. Lexing each piece of
    :func:`_code_chunks` on its own (constructs spanning two pieces may be
    coloured as if they ended there) also keeps quadratic lexers fast. Code
    with a line too long to cut, or in a language of
    :data:`_UNBOUNDED_LEXERS`, is formatted without colours, here. The input
    is prepared as :meth:`Lexer.get_tokens` does.
    """
    if clock.expired():
        return None
    text = lexer._preprocess_lexer_input(code)
    chunks = _code_chunks(text)
    if chunks is None or type(lexer).__name__ in _UNBOUNDED_LEXERS:
        lexer, chunks = TextLexer(), [text]
    if isinstance(lexer, TextLexer):
        return highlighter.render(chunks, lexer, formatter)
    html, spent, out_of_time, no_helper = highlighter.highlight(chunks, lexer, formatter, clock.left)
    clock.left = 0.0 if out_of_time else max(0.0, clock.left - spent)
    if html is None:
        clock.cut += 1
        clock.busy = clock.busy or no_helper
    return html


class _BoundedHilite(CodeHilite):
    """Python-Markdown's highlighter through :func:`_highlight`: :meth:`hilite` is None for plain code.

    Only the parts of upstream's ``hilite`` this renderer uses: Pygments, no
    guessing, the HTML formatter.
    """

    clock: _HighlightClock

    def hilite(self, shebang: bool = True) -> str | None:  # type: ignore[override]
        self.src = self.src.strip("\n")
        if self.lang is None and shebang:
            self._parseHeader()
        try:
            lexer = get_lexer_by_name(self.lang, **self.options)
        except ValueError:
            lexer = get_lexer_by_name("text", **self.options)
        if not self.lang:
            self.lang = lexer.aliases[0]
        return _highlight(self.src, lexer, HtmlFormatter(**self.options), self.clock)


class _SafeFencedBlocks(FencedBlockPreprocessor):
    """Keep author display options from becoming arbitrary Pygments kwargs."""

    budget: _HighlightBudget

    @staticmethod
    def _highlight_lines(value: str) -> list[int]:
        return [int(token) for token in value.split(maxsplit=MAX_HIGHLIGHT_LINES)[:MAX_HIGHLIGHT_LINES]
                if token.isascii() and token.isdecimal() and len(token) <= 7
                and 1 <= int(token) <= MAX_SOURCE_CHARS]

    def run(self, lines: list[str]) -> list[str]:
        # Older documents can specify hl_lines outside the attribute list.
        # Apply the same limit before the upstream preprocessor parses it.
        def bounded_fence(match: re.Match[str]) -> str:
            result = match.group(0)
            value = match.group("hl_lines")
            if value is not None:
                start, end = match.span("hl_lines")
                start -= match.start()
                end -= match.start()
                result = (result[:start] + " ".join(map(str, self._highlight_lines(value)))
                          + result[end:])
            code = match.group("code")
            if len(code) <= MAX_HIGHLIGHT_CODE_CHARS and self.budget.take(code):
                highlighted = self._highlighted(match, result)
                if highlighted is not None:
                    return highlighted
            # Large snippets, every block past the document's budget, and code
            # left plain for want of time remain literal code. Highlighting can
            # expand a short source into millions of span nodes and HTML bytes.
            attrs = match.group("attrs")
            if attrs is None:
                language = match.group("lang")
                attrs = "." + language if language else ""
            opening = match.group("fence") + "{" + attrs + " use_pygments=false}"
            return opening + result[result.index("\n"):]

        if not self.checked_for_deps:
            # As upstream's run does before its first block.
            for extension in self.md.registeredExtensions:
                if isinstance(extension, CodeHiliteExtension):
                    self.codehilite_conf = extension.getConfigs()
        # Rewrite exactly the blocks the upstream search extracts: it also
        # finds blocks inside an opening it skipped for malformed attributes.
        out: list[str] = []
        start = 0
        for first, last in _fenced_code(lines)[0]:
            block = "\n".join(lines[first:last + 1])
            match = self.FENCED_BLOCK_RE.fullmatch(block)
            out.extend(lines[start:first])
            out.extend((bounded_fence(match) if match else block).split("\n"))
            start = last + 1
        out.extend(lines[start:])
        return super().run(out)

    def _highlighted(self, match: re.Match[str], block: str) -> str | None:
        """*block* highlighted as upstream's run would, stashed: its placeholder line; None for plain code.

        Upstream would call Pygments on the whole block, so blocks to be
        highlighted are done here (through :class:`_BoundedHilite`), and the
        others reach it marked ``use_pygments=false``.
        """
        language, classes, config = None, [], {}
        if match.group("attrs"):
            attrs, _remainder = get_attrs_and_remainder(match.group("attrs"))  # never malformed: see _fenced_code
            _id, classes, config = self.handle_attrs(attrs)
            if classes:
                language = classes.pop(0)
        else:
            language = match.group("lang") or None
            if match.group("hl_lines"):
                config["hl_lines"] = self._highlight_lines(match.group("hl_lines"))
        if not (self.codehilite_conf and self.codehilite_conf["use_pygments"] and config.get("use_pygments", True)):
            return block  # upstream writes it as plain code
        local_config = {**self.codehilite_conf, **config}
        if classes:
            local_config["css_class"] = "{} {}".format(" ".join(classes), local_config["css_class"])
        hiliter = _BoundedHilite(match.group("code"), lang=language,
                                 style=local_config.pop("pygments_style", "default"), **local_config)
        hiliter.clock = self.budget.clock
        html = hiliter.hilite(shebang=False)
        if html is None:
            self.budget.cut()
            return None
        return "\n" + self.md.htmlStash.store(html) + "\n"

    def handle_attrs(self, attrs: Iterable[tuple[str, str]]) -> tuple[str, list[str], dict[str, object]]:
        element_id = ""
        classes = []
        options: dict[str, object] = {"guess_lang": False}
        for key, value in attrs:
            if key == "id":
                element_id = value
            elif key == ".":
                classes.append(value)
            elif key in ("linenos", "use_pygments"):
                normalized = value.lower()
                if normalized in ("true", "yes", "on", "1"):
                    options[key] = True
                elif normalized in ("false", "no", "off", "0"):
                    options[key] = False
            elif key in ("tabsize", "linenostart"):
                if value.isascii() and value.isdecimal() and len(value) <= 10:
                    ceiling = 16 if key == "tabsize" else MAX_SOURCE_CHARS
                    options[key] = max(1, min(int(value), ceiling))
            elif key == "hl_lines":
                options[key] = self._highlight_lines(value)
        return element_id, classes, options


class _SafeFencedCode(FencedCodeExtension):
    def __init__(self, budget: _HighlightBudget, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.budget = budget

    def extendMarkdown(self, md):  # noqa: N802 - Python-Markdown API name
        md.registerExtension(self)
        blocks = _SafeFencedBlocks(md, self.getConfigs())
        blocks.budget = self.budget
        md.preprocessors.register(blocks, "fenced_code_block", 25)


class _BudgetedHilite(HiliteTreeprocessor):
    """Highlight indented code blocks (``#!lang`` picks a lexer) within the document's budget."""

    budget: _HighlightBudget

    def run(self, root: Element) -> None:
        # Upstream's loop, with the budget checked before each block.
        for block in list(root.iter("pre")):
            if len(block) != 1 or block[0].tag != "code" or block[0].text is None:
                continue
            code = self.code_unescape(block[0].text)
            if len(code) > MAX_HIGHLIGHT_CODE_CHARS or not self.budget.take(code):
                continue  # stays literal code
            config = self.config.copy()
            highlighter = _BoundedHilite(code, tab_length=self.md.tab_length,
                                         style=config.pop("pygments_style", "default"), **config)
            highlighter.clock = self.budget.clock
            html = highlighter.hilite()
            if html is None:
                self.budget.cut()
                continue
            placeholder = self.md.htmlStash.store(html)
            block.clear()
            block.tag = "p"  # removed again when the stashed HTML is inserted
            block.text = placeholder


class _BudgetedCodeHilite(CodeHiliteExtension):
    def __init__(self, budget: _HighlightBudget, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.budget = budget

    def extendMarkdown(self, md):  # noqa: N802 - Python-Markdown API name
        hiliter = _BudgetedHilite(md)
        hiliter.config = self.getConfigs()
        hiliter.budget = self.budget
        md.treeprocessors.register(hiliter, "hilite", 30)
        md.registerExtension(self)


class _FirstTocMarker(TocTreeprocessor):
    """Only the first ``[TOC]`` marker becomes the table; later ones stay literal text.

    Upstream copies the table into every marker and finds each marker by
    scanning its siblings, so a short source repeating the marker would grow
    the output with markers × headings and the work with markers squared.
    """

    def replace_marker(self, root: Element, elem: Element) -> None:
        for parent, child in self.iterparent(root):
            # Upstream's test: the marker as the only content of an element.
            if child.text and child.text.strip() == self.marker and len(child) == 0:
                parent[list(parent).index(child)] = elem
                return


def _heading_slug(value: str, separator: str) -> str:
    slug = _toc_slugify(value, separator)
    return slug + "_1" if slug in RESERVED_IDS else slug


class _TableOfContents(TocExtension):
    TreeProcessorClass = _FirstTocMarker

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("slugify", _heading_slug)
        super().__init__(**kwargs)


class _OutputTooLarge(Exception):
    """The document's HTML would be longer than :data:`MAX_HTML_CHARS`."""


class _BoundedOutput(Treeprocessor):
    """Stop before serialising a tree whose HTML would exceed :data:`MAX_HTML_CHARS`.

    Every use of a reference link repeats its definition's URL and title, so a
    small source can expand into gigabytes. The uses share those strings: each
    distinct string is measured once, keeping this walk linear in the source.
    """

    def __init__(self, md, limit: int = MAX_HTML_CHARS) -> None:
        super().__init__(md)
        self.limit = limit

    def run(self, root: Element) -> None:
        measured: dict[int, int] = {}

        def size(value: str | None) -> int:
            if not value:
                return 0
            known = measured.get(id(value))
            if known is None:
                # Room for the entities the serialiser may write.
                known = measured[id(value)] = (len(value) + 4 * value.count("&") + 5 * value.count('"')
                                               + 3 * (value.count("<") + value.count(">")))
            return known

        # Stashed raw HTML (highlighted code, inline tags) is inserted unchanged.
        total = sum(len(block) for block in self.md.htmlStash.rawHtmlBlocks if isinstance(block, str))
        for element in root.iter():
            tag = element.tag if isinstance(element.tag, str) else ""
            total += 2 * len(tag) + 5 + size(element.text) + size(element.tail)
            for name, value in element.attrib.items():
                total += len(name) + 4 + size(value)
            if total > self.limit:
                raise _OutputTooLarge


class _OutputLimit(Extension):
    def __init__(self, limit: int = MAX_HTML_CHARS) -> None:
        super().__init__()
        self.limit = limit

    def extendMarkdown(self, md):  # noqa: N802 - Python-Markdown API name
        # After the table of contents (5), the last step that adds elements.
        md.treeprocessors.register(_BoundedOutput(md, self.limit), "bw_output_limit", 1)


_TASK_ITEM = re.compile(r"<li>(\s*<p>)?\s*\[( |x|X)\]\s+")


def _task_lists(html: str) -> str:
    """``- [ ] item`` / ``- [x] item`` become read-only check marks.

    The mark is real text (not aria-hidden): screen readers announce "ballot box" / "ballot box with
    check" in the reader's own language, so the done state is not conveyed by colour alone. The box
    has a fixed width (content.css), so no space follows it and wrapped lines line up with the text.
    """

    def replace(match: re.Match[str]) -> str:
        done = match.group(2) in "xX"
        mark = "☑" if done else "☐"
        state = "done" if done else "open"
        return (f'<li class="task-item task-{state}">{match.group(1) or ""}'
                f'<span class="task-box">{mark}</span>')

    return _TASK_ITEM.sub(replace, html)


_TABLE_DATE = re.compile(r"(<td(?: [^>]*)?>)\s*(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)\s*(</td>)")


def _nowrap_table_dates(html: str) -> str:
    """A table cell holding only an ISO date keeps it on one line (never "2026-09-" / "01")."""
    return _TABLE_DATE.sub(r'\1<span class="nowrap">\2</span>\3', html)


_CacheKey = tuple[object, ...]


class _RenderCache:
    """Renderings by kind, options and source hash, least recently used dropped first.

    Entries are kept UTF-8 encoded: a str holding one character above U+FFFF
    takes 4 bytes for each of its characters, the encoded markup about one.
    """

    def __init__(self) -> None:
        self._items: OrderedDict[_CacheKey, bytes] = OrderedDict()
        self.size = 0
        self._lock = threading.Lock()
        self._making: dict[_CacheKey, list] = {}  # key: [lock, threads using it]

    @contextmanager
    def making(self, key: _CacheKey) -> Iterator[None]:
        """Hold the turn to make *key*'s rendering: concurrent first readers wait for it, then read it."""
        with self._lock:
            entry = self._making.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._lock:
                entry[1] -= 1
                if not entry[1]:
                    del self._making[key]

    @staticmethod
    def _footprint(value: bytes) -> int:
        return sys.getsizeof(value) + 256

    def get(self, key: _CacheKey) -> str | None:
        with self._lock:
            value = self._items.get(key)
            if value is None:
                return None
            self._items.move_to_end(key)
        return value.decode("utf-8", "surrogatepass")

    def put(self, key: _CacheKey, html: str) -> None:
        value = html.encode("utf-8", "surrogatepass")
        footprint = self._footprint(value)
        with self._lock:
            if footprint > RENDER_CACHE_LIMIT // 2:
                return
            old = self._items.pop(key, None)
            if old is not None:
                self.size -= self._footprint(old)
            self._items[key] = value
            self.size += footprint
            while self.size > RENDER_CACHE_LIMIT and self._items:
                _key, dropped = self._items.popitem(last=False)
                self.size -= self._footprint(dropped)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.size = 0


_render_cache = _RenderCache()


def clear_render_cache() -> None:
    """Forget every kept rendering (tests; nothing else needs to: keys are source hashes)."""
    _render_cache.clear()


def _source_hash(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()


def render(
    text: str | None,
    *,
    embed_videos: bool = True,
    fix_lists: bool = True,
    mentions: bool = True,
    profile_url: Callable[[str], str] = _default_profile_url,
    highlight: bool = True,
    keep: bool = False,
) -> str:
    """Render stored Markdown to sanitised HTML.

    The result depends only on the source and the options, so renderings of
    sources of :data:`RENDER_CACHE_MIN_CHARS` or more, or that highlighted
    code, are kept by their hash (a custom *profile_url* is not part of the
    key: those are never kept). With *keep*, shorter ones are kept too: for
    lists that render many short documents on every request (the comments of
    a ticket). A rendering that a :func:`shared_highlighting` allowance cut
    short is not kept either. Without *highlight*, code stays plain and uses
    no allowance (for text that loses its markup anyway). Inside
    :func:`one_document`, nothing is kept and the limits are shared.
    """
    if not text:
        return ""
    if len(text) > MAX_SOURCE_CHARS:
        text = text[:MAX_SOURCE_CHARS]
    shared = _shared_budget.get() if highlight else None
    allowance = MAX_HIGHLIGHT_DOCUMENT_CHARS if highlight else 0
    limits = _shared_limits.get()
    if profile_url is not _default_profile_url or limits is not None:
        return _render(text, embed_videos, fix_lists, mentions, profile_url, shared or _HighlightBudget(allowance),
                       limits)
    key = ("markdown", _source_hash(text), embed_videos, fix_lists, mentions, highlight)
    html = _render_cache.get(key)
    if html is not None:
        return html
    with _render_cache.making(key):
        html = _render_cache.get(key)
        if html is not None:
            return html
        budget = shared or _HighlightBudget(allowance)
        left, refused = budget.left, budget.refused
        html = _render(text, embed_videos, fix_lists, mentions, profile_url, budget)
        costly = keep or len(text) >= RENDER_CACHE_MIN_CHARS or budget.left != left
        # A rendering cut short by an allowance or time shared with other documents depends on them.
        independent = shared is None and _shared_clock.get() is None
        if costly and (independent or budget.refused == refused) and not budget.clock.busy:
            _render_cache.put(key, html)
    return html


def _render(text: str, embed_videos: bool, fix_lists: bool, mentions: bool,
            profile_url: Callable[[str], str], budget: _HighlightBudget | None = None,
            limits: _DocumentLimits | None = None) -> str:
    used: list[int] = []
    exceeded = limits.exceeded if limits is not None else lambda source, _used: _parser_work_exceeded(source)
    if exceeded(text, used):
        return _plain_source(text)
    prepared = _preprocess(text, fix_lists)
    # Preserved blank lines introduce <br> tags. Bound the actual parser
    # input too, so generated inline markup cannot bypass the source check.
    if prepared != text and exceeded(prepared, used):
        return _plain_source(text)
    html_limit = MAX_HTML_CHARS
    if limits is not None:
        limits.take(used)
        html_limit = limits.html
    # Fenced and indented code share one highlighting allowance.
    if budget is None:
        budget = _HighlightBudget()
    try:
        html = _markdown.markdown(
            prepared,
            extensions=["tables", _SafeFencedCode(budget),
                        _BudgetedCodeHilite(budget, css_class="codehilite", guess_lang=False),
                        _TableOfContents(), "nl2br", _Strikethrough(), _OutputLimit(html_limit)],
            output_format="html",
        )
    except (RecursionError, _OutputTooLarge):
        # Keep imported content readable if an upstream extension encounters
        # an additional recursive construct not covered by the preflight, or
        # the document expands beyond the output limit.
        return _plain_source(text)
    html = _nowrap_table_dates(_task_lists(_escape_attribute_brackets(sanitize(html))))
    embeds = limits.embeds if limits is not None else _EmbedBudget()
    html = _embed_videos(html, embed_videos, embeds)
    html = _embed_boards(html, embeds)
    if mentions:
        html = _link_mentions(html, profile_url)
    # Embeds and mentions lengthen the HTML after the tree was measured.
    size = _utf8_size(html)
    if len(html) > html_limit or size > min(html_limit, MAX_HTML_BYTES):
        return _plain_source(text)
    if limits is not None and not limits.fits(html):
        return _plain_source(text)
    return html


def _utf8_size(text: str) -> int:
    return len(text) if text.isascii() else len(text.encode("utf-8", "surrogatepass"))


def highlight_code(code: str | None, language: str | None = None) -> str:
    """Syntax-highlight a code snippet (code custom pages, builder code blocks, the editor).

    Results are cached like :func:`render`'s; inside :func:`shared_highlighting`
    a snippet past the shared allowance stays plain code, and so does one
    whose HTML no longer fits inside :func:`one_document`. Highlighting has the
    time of a document (:data:`MAX_HIGHLIGHT_SECONDS`), or the time shared by
    :func:`bounded_highlighting`; past it the snippet stays plain code.
    """
    code = "" if code is None else str(code)[:MAX_SOURCE_CHARS]
    plain = _plain_code(code)
    if len(code) > MAX_HIGHLIGHT_CODE_CHARS:
        return plain
    html = _highlight_code(code, (language or "").strip(), plain)
    limits = _shared_limits.get()
    if html is not plain and limits is not None and not limits.fits(html):
        return plain
    return html


def _plain_code(code: str) -> str:
    return "<pre><code>" + escape(code) + "</code></pre>"


def _highlight_code(code: str, language: str, plain: str) -> str:
    key = ("code", _source_hash(code), language)
    cached = _render_cache.get(key)
    if cached is not None:
        return cached
    with _render_cache.making(key):
        cached = _render_cache.get(key)
        if cached is not None:
            return cached
        shared = _shared_budget.get()
        if shared is not None and not shared.take(code):
            return plain
        clock = shared.clock if shared is not None else _shared_clock.get() or _HighlightClock()
        try:
            if language:
                lexer = get_lexer_by_name(language, stripall=False)
            elif code.strip():
                lexer = guess_lexer(code[:MAX_LEXER_SAMPLE_CHARS])
            else:
                lexer = TextLexer()
        except ClassNotFound:
            lexer = TextLexer()
        html = _highlight(code, lexer, HtmlFormatter(cssclass="codehilite"), clock)
        if html is None:
            if shared is not None:
                shared.cut()
            if shared is not None or _shared_clock.get() is not None or clock.busy:
                return plain  # depends on the other blocks of the answer, or on busy helpers: not kept
            html = plain
        else:
            html = sanitize(html)
        _render_cache.put(key, html)
    return html


def render_parts(parts: list[tuple[str, str] | tuple[str, str, str]]) -> list[str]:
    """Render the parts of one page as one document (:func:`one_document`), in order.

    A part is ``("markdown", text)`` (:func:`render`) or ``("code", code,
    language)`` (:func:`highlight_code`). For a page made of many blocks (the
    page builder): together they get the limits and highlighting of one
    document, their HTML (highlighted code and blocks shown as source too)
    within one document's, and the renderings are kept together by the hash of all parts,
    so that a page read again is not rendered again, however short its
    blocks. A rendering cut short by time shared with other documents, or by
    busy highlighting helpers, is not kept.
    """
    if not parts:
        return []
    key = ("parts", _source_hash(json.dumps(parts, ensure_ascii=False)))
    cached = _render_cache.get(key)
    if cached is not None:
        return _split_parts(cached)
    with _render_cache.making(key):
        cached = _render_cache.get(key)
        if cached is not None:
            return _split_parts(cached)
        outer = _shared_clock.get()
        cut = outer.cut if outer is not None else 0
        # What every part would take shown as source is kept aside, so that the renderings
        # and the fallbacks together stay within one document's HTML and the entry is kept.
        reserve = sum(_utf8_size(_plain_source(part[1][:MAX_SOURCE_CHARS]) if part[0] == "markdown"
                                 else _plain_code(str(part[1])[:MAX_SOURCE_CHARS])) for part in parts)
        with one_document(reserve):
            clock = _shared_clock.get()
            html = [render(part[1]) if part[0] == "markdown" else highlight_code(part[1], part[2])
                    for part in parts]
        if clock is not None and not clock.busy and (outer is None or clock.cut == cut):
            # Their lengths, then the renderings: within one document's HTML (see *reserve*).
            _render_cache.put(key, ",".join(str(len(part)) for part in html) + "\n" + "".join(html))
    return html


def _split_parts(kept: str) -> list[str]:
    lengths, _, joined = kept.partition("\n")
    parts, start = [], 0
    for length in map(int, lengths.split(",")):
        parts.append(joined[start:start + length])
        start += length
    return parts


_STRIP_TAGS = re.compile(r"<[^>]+>")
# Tags that end a block of text: they become a space. Inline tags (<strong>, <a>, <code>…) vanish,
# so "before **v1.7.0**." stays "before v1.7.0." instead of gaining a space before the period.
_BLOCK_TAGS = re.compile(
    r"</?(?:p|div|br|hr|li|ul|ol|dl|dt|dd|h[1-6]|blockquote|pre|table|thead|tbody|tfoot|tr|td|th|caption"
    r"|figure|figcaption|details|summary|section|article|aside|header|footer|nav|img|iframe|video|audio)\b[^>]*>",
    re.IGNORECASE,
)


def to_plain_text(text: str | None) -> str:
    """Rendered text without markup (search snippets, previews, outlines).

    Code is not highlighted: its markup would be stripped right away, and
    inside :func:`shared_highlighting` it would use up the allowance of the
    page that shows the text (a page list of a page-builder page).
    """
    from html import unescape

    html = render(text, embed_videos=False, mentions=False, highlight=False)
    text = _STRIP_TAGS.sub("", _BLOCK_TAGS.sub(" ", html))
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _source_head(text: str, limit: int) -> str:
    """The lines of *text* that fit in *limit* characters, closing a code fence left open."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    line_end = head.rfind("\n")
    if line_end > 0:
        head = head[:line_end]
    unclosed = _fenced_code(_normalise_whitespace(head).split("\n"))[2]
    if unclosed:
        # A long code block at the start stays code, not fence markers and text.
        head += "\n" + unclosed
    return head


def excerpt(text: str | None, length: int = 200, *, keep: bool = False) -> str:
    """Plain-text preview of at most *length* characters.

    Only the start of the source is rendered, within a small work allowance,
    so a list of many pages costs the same however long the pages are. A
    dense start is retried shorter; a preview of a cut source ends in "…".
    With *keep*, the preview is kept by the source's hash like a rendering:
    for lists that show many excerpts on every request (the Kanban boards).
    """
    if not keep or not text:
        return _excerpt(text, length)
    key = ("excerpt", _source_hash(text), length)
    cached = _render_cache.get(key)
    if cached is None:
        cached = _excerpt(text, length)
        _render_cache.put(key, cached)
    return cached


def _excerpt(text: str | None, length: int) -> str:
    source = text or ""
    for limit in (EXCERPT_SOURCE_CHARS, EXCERPT_SOURCE_CHARS // 4):
        head = _source_head(source, limit)
        # Weigh the parser input too: blank-line runs become <br> tags.
        if not any(_parser_work_exceeded(form, MAX_EXCERPT_WORK, MAX_EXCERPT_BLOCK_COST, MAX_EXCERPT_HEADINGS)
                   for form in (head, _preprocess(head, True))):
            plain = to_plain_text(head)
            break
    else:
        plain = re.sub(r"\s+", " ", head).strip()
    if len(plain) <= length and (len(source) <= limit or not plain):
        return plain
    return plain[: length - 1].rstrip() + "…"
