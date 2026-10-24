"""Markdown files with a small YAML-style front matter, as 1.4 wrote and read them.

::

    ---
    title: "Getting started"
    slug: "getting-started"
    category_path: "Guides/Basics"
    exported_at: "2024-05-01 10:00:00"
    ---

    # Getting started
    …

Only flat ``key: value`` lines are understood (quoted or not); anything else
in the front matter is ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_H1_START = re.compile(r"[ \t]*#[ \t]+")
_BOM = "\ufeff"


@dataclass(frozen=True)
class MarkdownFile:
    meta: dict[str, str]
    body: str


def quote(value: object) -> str:
    """A double-quoted scalar that round-trips colons, quotes and backslashes."""
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{text}"'


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        out, i = [], 1
        while i < len(value) - 1:
            char = value[i]
            if char == "\\" and i + 1 < len(value) - 1:
                i += 1
                char = value[i]
            out.append(char)
            i += 1
        return "".join(out)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def decode(raw: bytes) -> str:
    """Text of an uploaded file: UTF-8, or Latin-1 for files from older editors."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return text.removeprefix(_BOM).replace("\r\n", "\n").replace("\r", "\n")


def parse(text: str) -> MarkdownFile:
    match = _FRONT_MATTER.match(text)
    if not match:
        return MarkdownFile({}, text)
    meta: dict[str, str] = {}
    for line in match.group(1).split("\n"):
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if sep and key and not line.startswith((" ", "\t")):
            meta[key] = _unquote(value)
    return MarkdownFile(meta, text[match.end():].lstrip("\n"))


def _h1(body: str) -> tuple[int, int, str, str] | None:
    """``(start, end, line, text)`` of the first ``# Heading`` line, or None.

    Read line by line in linear time (a regular expression with a lazy group
    and an optional closing run took quadratic time on long runs of spaces).
    A closing run of ``#`` needs a space before it (CommonMark), so "# C#"
    keeps its "#".
    """
    position = 0
    for line in body.split("\n"):
        opening = _H1_START.match(line)
        if opening:
            text = line[opening.end():].rstrip(" \t")
            without = text.rstrip("#")
            if without != text and without[-1:] in (" ", "\t"):
                text = without.rstrip(" \t")
            if text:
                return position, position + len(line), line, text
        position += len(line) + 1
    return None


def first_heading(body: str) -> str | None:
    """The first ``# Heading`` when it opens the document (after blank lines only)."""
    found = _h1(body)
    if found and not body[:found[0]].strip():
        return found[3].strip()
    return None


def strip_heading(body: str, title: str) -> str:
    """Remove the opening ``# title`` line that export adds (and 1.4 files often have)."""
    found = _h1(body)
    if found and not body[:found[0]].strip() and (found[2].strip() == f"# {title}" or found[3].strip() == title):
        return body[found[1]:].lstrip("\n")
    return body


def document(meta: dict[str, object], body: str, *, heading: str | None = None) -> str:
    """A Markdown file with front matter, optionally opening with ``# heading``.

    The heading is added even when the body starts with the same one: the
    import removes exactly one, so the body round-trips unchanged.
    """
    lines = ["---", *(f"{key}: {quote(value)}" for key, value in meta.items()), "---", ""]
    top = f"# {heading}\n\n" if heading else ""
    return "\n".join(lines) + "\n" + top + (body or "")
